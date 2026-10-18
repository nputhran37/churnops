"""
Warehouse abstraction for the P1 tables defined in contracts/bq/.

Local mode writes partitioned parquet under data/warehouse/<table>/, BigQuery mode
(CHURNOPS_USE_BQ=1) writes to <project>.<dataset>.<table>. Both honour the same
idempotency rule the PRD asks for: a batch is MERGEd on batch_id, so re-running an
ingest never duplicates rows.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
from dataclasses import dataclass

import pandas as pd

from .contracts import load_bq_schema

PARTITIONED_TABLES = {"raw_events", "features_daily", "labels"}
APPEND_TABLES = {"drift_reports", "reference_window", "predictions_log", "llm_calls"}

# Primary keys used for idempotent merges.
MERGE_KEYS: dict[str, list[str]] = {
    "raw_events": ["batch_id", "customer_id"],
    "features_daily": ["batch_id", "customer_id"],
    "labels": ["batch_id", "customer_id"],
    "drift_reports": ["report_id"],
    "reference_window": ["model_version", "feature_name"],
}


def use_bigquery() -> bool:
    return os.getenv("CHURNOPS_USE_BQ", "0").lower() in ("1", "true", "yes")


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Warehouse:
    root: str = os.path.join("data", "warehouse")
    project: str = ""
    dataset: str = ""
    remote: bool = False

    @classmethod
    def default(cls, root: str | None = None) -> Warehouse:
        return cls(
            root=root or os.getenv("CHURNOPS_WAREHOUSE_DIR", os.path.join("data", "warehouse")),
            project=os.getenv("CHURNOPS_PROJECT_ID", "churnops-dev"),
            dataset=os.getenv("CHURNOPS_BQ_DATASET", "churnops"),
            remote=use_bigquery(),
        )

    # ------------------------------------------------------------------
    def table_ref(self, table: str) -> str:
        return f"{self.project}.{self.dataset}.{table}"

    def _client(self):  # pragma: no cover - requires GCP
        from google.cloud import bigquery

        return bigquery.Client(project=self.project)

    def _table_dir(self, table: str) -> str:
        return os.path.join(self.root, table)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def merge_batch(self, table: str, df: pd.DataFrame, batch_id: str, batch_date: str | None = None) -> dict:
        """Idempotent load of one batch. Re-running replaces the batch's rows."""
        if df.empty:
            return {"table": table, "rows": 0, "path": None, "mode": "skipped-empty"}

        if self.remote:  # pragma: no cover - requires GCP
            return self._merge_batch_bq(table, df, batch_id)

        batch_date = batch_date or str(df.get("batch_date", pd.Series(["unknown"])).iloc[0])
        partition_dir = os.path.join(self._table_dir(table), f"date={batch_date}")
        os.makedirs(partition_dir, exist_ok=True)

        # One file per batch_id is the local equivalent of MERGE on batch_id: a rerun
        # overwrites that file and leaves every other batch untouched.
        target = os.path.join(partition_dir, f"{table}_{batch_id}.parquet")
        existed = os.path.exists(target)
        df.to_parquet(target, index=False)
        return {
            "table": table,
            "rows": len(df),
            "path": target,
            "mode": "replaced" if existed else "inserted",
        }

    def _merge_batch_bq(self, table: str, df: pd.DataFrame, batch_id: str) -> dict:  # pragma: no cover
        from google.cloud import bigquery

        client = self._client()
        staging = f"{self.table_ref(table)}_staging_{batch_id.replace('-', '_')}"
        job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
        client.load_table_from_dataframe(df, staging, job_config=job_config).result()

        keys = MERGE_KEYS.get(table, ["batch_id"])
        on_clause = " AND ".join(f"target.{k} = source.{k}" for k in keys)
        columns = list(df.columns)
        # DataFrame loads infer Python strings as BigQuery STRING, even when the
        # destination contract declares a DATE (notably batch_date). Cast values
        # using the destination schema so the staging table can be safely merged.
        target_fields = {field.name: field.field_type.upper() for field in client.get_table(self.table_ref(table)).schema}

        def source_value(column: str) -> str:
            field_type = target_fields.get(column)
            if field_type in {"DATE", "DATETIME", "TIME", "TIMESTAMP"}:
                return f"SAFE_CAST(source.{column} AS {field_type})"
            return f"source.{column}"

        update = ", ".join(f"target.{c} = {source_value(c)}" for c in columns if c not in keys)
        insert_cols = ", ".join(columns)
        insert_vals = ", ".join(source_value(c) for c in columns)
        sql = (
            f"MERGE `{self.table_ref(table)}` AS target USING `{staging}` AS source ON {on_clause} "
            f"WHEN MATCHED THEN UPDATE SET {update} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        client.query(sql).result()
        client.delete_table(staging, not_found_ok=True)
        return {"table": table, "rows": len(df), "path": self.table_ref(table), "mode": "merged"}

    def insert_row(self, table: str, row: dict) -> dict:
        """Append (or replace by primary key) a single summary row, e.g. drift_reports."""
        validated = self._coerce_to_schema(table, row)
        if self.remote:  # pragma: no cover - requires GCP
            errors = self._client().insert_rows_json(self.table_ref(table), [validated])
            if errors:
                raise RuntimeError(f"BigQuery insert into {table} failed: {errors}")
            return {"table": table, "rows": 1, "path": self.table_ref(table)}

        os.makedirs(self._table_dir(table), exist_ok=True)
        path = os.path.join(self._table_dir(table), f"{table}.jsonl")
        keys = MERGE_KEYS.get(table, [])
        existing = self.read_rows(table)
        if keys:
            existing = [r for r in existing if any(r.get(k) != validated.get(k) for k in keys)]
        existing.append(validated)
        with open(path, "w", encoding="utf-8") as handle:
            for record in existing:
                handle.write(json.dumps(record, default=str) + "\n")
        return {"table": table, "rows": 1, "path": path}

    def update_row(self, table: str, keys: dict, updates: dict) -> bool:
        """Patch an existing summary row (used to backfill realised_auc when labels land)."""
        if self.remote:  # pragma: no cover - requires GCP
            set_clause = ", ".join(f"{k} = @u_{k}" for k in updates)
            where = " AND ".join(f"{k} = @k_{k}" for k in keys)
            from google.cloud import bigquery

            params = [bigquery.ScalarQueryParameter(f"u_{k}", _bq_type(v), v) for k, v in updates.items()]
            params += [bigquery.ScalarQueryParameter(f"k_{k}", _bq_type(v), v) for k, v in keys.items()]
            config = bigquery.QueryJobConfig(query_parameters=params)
            sql = f"UPDATE `{self.table_ref(table)}` SET {set_clause} WHERE {where}"
            self._client().query(sql, job_config=config).result()
            return True

        rows = self.read_rows(table)
        changed = False
        for row in rows:
            if all(row.get(k) == v for k, v in keys.items()):
                row.update(updates)
                changed = True
        if changed:
            path = os.path.join(self._table_dir(table), f"{table}.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for record in rows:
                    handle.write(json.dumps(record, default=str) + "\n")
        return changed

    def replace_table(self, table: str, rows: list[dict]) -> dict:
        """Rewrite a whole small table (reference_window refresh)."""
        if self.remote:  # pragma: no cover - requires GCP
            client = self._client()
            client.query(f"TRUNCATE TABLE `{self.table_ref(table)}`").result()
            if rows:
                errors = client.insert_rows_json(self.table_ref(table), rows)
                if errors:
                    raise RuntimeError(f"BigQuery insert into {table} failed: {errors}")
            return {"table": table, "rows": len(rows), "path": self.table_ref(table)}

        os.makedirs(self._table_dir(table), exist_ok=True)
        path = os.path.join(self._table_dir(table), f"{table}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(self._coerce_to_schema(table, row), default=str) + "\n")
        return {"table": table, "rows": len(rows), "path": path}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def read_rows(self, table: str) -> list[dict]:
        if self.remote:  # pragma: no cover - requires GCP
            return [dict(r) for r in self._client().query(f"SELECT * FROM `{self.table_ref(table)}`").result()]
        path = os.path.join(self._table_dir(table), f"{table}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def read_table(self, table: str, batch_date: str | None = None, limit_partitions: int | None = None) -> pd.DataFrame:
        """Read a partitioned table back as a DataFrame (newest partitions first)."""
        if self.remote:  # pragma: no cover - requires GCP
            where = f" WHERE batch_date = '{batch_date}'" if batch_date else ""
            return self._client().query(f"SELECT * FROM `{self.table_ref(table)}`{where}").to_dataframe()

        pattern = os.path.join(self._table_dir(table), f"date={batch_date}" if batch_date else "date=*", "*.parquet")
        files = sorted(glob.glob(pattern), reverse=True)
        if limit_partitions:
            files = files[:limit_partitions]
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    # ------------------------------------------------------------------
    def _coerce_to_schema(self, table: str, row: dict) -> dict:
        """Keep only contract columns and fill required timestamps."""
        try:
            schema = load_bq_schema(table)
        except FileNotFoundError:
            return dict(row)
        allowed = {f["name"] for f in schema}
        coerced = {k: v for k, v in row.items() if k in allowed}
        for field_def in schema:
            missing_timestamp = (
                field_def.get("mode") == "REQUIRED"
                and field_def["name"] not in coerced
                and field_def["type"] == "TIMESTAMP"
            )
            if missing_timestamp:
                coerced[field_def["name"]] = _utc_now()
        return coerced


def _bq_type(value) -> str:  # pragma: no cover - requires GCP
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    return "STRING"
