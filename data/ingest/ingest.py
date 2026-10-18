"""
ChurnOps ingestion job (Person 1, Phase C).

Runs as a Cloud Run Job, triggered by Eventarc on object-finalize in the landing
bucket (or from the CLI for local runs and backfills):

  landed Parquet
      -> Pandera validation generated from contracts/features.yaml
      -> batch-level data-quality rules (volume, null rate, duplicate keys)
      -> FAIL: copy to gs://<project>-quarantine/, write a structured error, raise
               a data.quality alert log, and stop. Nothing reaches the warehouse.
      -> PASS: MERGE into raw_events (idempotent on batch_id + customer_id)
               join any labels that have already arrived
               build features_daily with the shared transform
               publish data.batch.landed
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.contracts import load_feature_contract  # noqa: E402
from common.events import EventPublisher, build_event  # noqa: E402
from common.features import FeatureTransformer  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402
from common.storage import Storage, read_tabular  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from schemas import build_raw_events_schema, check_batch_quality  # noqa: E402

log = get_logger("ingest")


class BatchQuarantined(RuntimeError):
    """Raised when a batch fails validation and is routed to the quarantine bucket."""

    def __init__(self, batch_id: str, reasons: list[dict], quarantine_uri: str):
        super().__init__(f"Batch {batch_id} quarantined ({len(reasons)} issue(s))")
        self.batch_id = batch_id
        self.reasons = reasons
        self.quarantine_uri = quarantine_uri


def _scalar(df: pd.DataFrame, column: str, default: str) -> str:
    if column in df.columns and len(df) and pd.notna(df[column].iloc[0]):
        return str(df[column].iloc[0])
    return default


def _pandera_failures(exc: Exception) -> list[dict]:
    """Flatten a Pandera SchemaErrors object into contract-shaped reason rows."""
    failure_cases = getattr(exc, "failure_cases", None)
    if failure_cases is None or not hasattr(failure_cases, "head"):
        return [{"rule": "schema", "column": "*", "observed": str(exc)[:500], "threshold": "contract", "message": str(exc)[:500]}]

    reasons: list[dict] = []
    grouped = failure_cases.groupby(["column", "check"], dropna=False).size().reset_index(name="count")
    for _, row in grouped.head(25).iterrows():
        reasons.append(
            {
                "rule": "pandera_check",
                "column": str(row["column"]),
                "observed": int(row["count"]),
                "threshold": str(row["check"]),
                "message": f"{int(row['count'])} row(s) failed '{row['check']}' on column '{row['column']}'",
            }
        )
    return reasons


def quarantine_batch(
    batch_file_path: str,
    batch_id: str,
    batch_date: str,
    trace_id: str,
    reasons: list[dict],
    quarantine: Storage,
    row_count: int,
) -> dict:
    """Park the batch and the structured error next to each other for the runbook."""
    object_prefix = f"date={batch_date}"
    stored_path = quarantine.copy_in(batch_file_path, f"{object_prefix}/{os.path.basename(batch_file_path)}")
    error_payload = {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "trace_id": trace_id,
        "row_count": row_count,
        "quarantined_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_object": batch_file_path,
        "issues": reasons,
        "runbook": "docs/runbooks/batch_quarantined.md",
    }
    error_path = quarantine.write_json(f"{object_prefix}/error_{batch_id}.json", error_payload)

    log.error(
        f"Batch {batch_id} FAILED validation and was quarantined",
        trace_id,
        batch_id=batch_id,
        quarantine_uri=quarantine.uri(f"{object_prefix}/{os.path.basename(batch_file_path)}"),
        error_object=error_path,
        issue_count=len(reasons),
        metrics={"quarantined_rows": row_count, "issues": reasons[:5]},
        alert="data_quality_quarantine",
    )
    return {
        "status": "quarantined",
        "batch_id": batch_id,
        "batch_date": batch_date,
        "trace_id": trace_id,
        "row_count": row_count,
        "issues": reasons,
        "quarantine_path": stored_path,
        "quarantine_uri": quarantine.uri(f"{object_prefix}/{os.path.basename(batch_file_path)}"),
        "error_path": error_path,
    }


def attach_arrived_labels(df: pd.DataFrame, warehouse: Warehouse, batch_date: str) -> tuple[pd.DataFrame, int]:
    """Fill churn for rows whose delayed labels have already been released."""
    labels = warehouse.read_table("labels", batch_date=batch_date)
    if labels.empty or "customer_id" not in labels.columns:
        return df, 0

    lookup = labels.set_index("customer_id")["churn"]
    filled = df["customer_id"].map(lookup)
    merged = df.copy()
    merged["churn"] = pd.to_numeric(
        merged["churn"] if "churn" in merged.columns else pd.Series([pd.NA] * len(merged)),
        errors="coerce",
    ).fillna(pd.to_numeric(filled, errors="coerce")).astype("Int64")
    return merged, int(merged["churn"].notna().sum())


def process_batch(
    batch_file_path: str,
    quarantine_dir: str | None = None,
    warehouse_dir: str | None = None,
    events_dir: str | None = None,
    raise_on_quarantine: bool = False,
) -> dict:
    """Validate and load one landed batch. Returns a status dict; never raises on bad data."""
    contract = load_feature_contract()
    warehouse = Warehouse.default(root=warehouse_dir)
    quarantine = Storage.for_role("quarantine", local_root=quarantine_dir)
    publisher = EventPublisher(outbox_dir=events_dir or EventPublisher.outbox_dir, service="ingest")

    if not batch_file_path.startswith("gs://") and not os.path.exists(batch_file_path):
        raise FileNotFoundError(f"Batch file not found: {batch_file_path}")

    df = read_tabular(batch_file_path)
    trace_id = _scalar(df, "trace_id", f"tr-{uuid.uuid4().hex[:12]}")
    batch_id = _scalar(df, "batch_id", f"b-unknown-{uuid.uuid4().hex[:6]}")
    batch_date = _scalar(df, "batch_date", datetime.date.today().isoformat())

    started = datetime.datetime.now(datetime.timezone.utc)
    log.info(
        f"Ingesting batch {batch_id} ({len(df)} rows) from {batch_file_path}",
        trace_id,
        batch_id=batch_id,
        row_count=len(df),
        source=batch_file_path,
    )

    # ---- 1. schema validation (per row) ------------------------------
    schema = build_raw_events_schema(contract)
    reasons: list[dict] = []
    validated = df
    try:
        validated = schema.validate(df, lazy=True)
    except Exception as exc:  # pandera.errors.SchemaErrors
        reasons.extend(_pandera_failures(exc))

    # ---- 2. batch-level data-quality rules ---------------------------
    quality = check_batch_quality(df, contract)
    reasons.extend(issue.as_dict() for issue in quality.issues)

    if reasons:
        result = quarantine_batch(
            batch_file_path, batch_id, batch_date, trace_id, reasons, quarantine, len(df)
        )
        if raise_on_quarantine:
            raise BatchQuarantined(batch_id, reasons, result["quarantine_uri"])
        return result

    log.info(
        f"Validation PASSED for batch {batch_id}",
        trace_id,
        batch_id=batch_id,
        metrics={"row_count": len(df), "null_fractions": quality.as_dict()["null_fractions"]},
    )
    # One line per imperfect feature so the data-quality dashboard can label by feature.
    for feature, fraction in quality.null_fractions.items():
        if fraction > 0:
            log.info(
                f"Null rate for {feature}: {fraction:.2%}",
                trace_id,
                batch_id=batch_id,
                metrics={"feature": feature, "null_fraction": round(fraction, 6)},
            )

    # ---- 3. idempotent load into raw_events --------------------------
    validated, labelled_rows = attach_arrived_labels(validated, warehouse, batch_date)
    if "ingested_at" not in validated.columns:
        validated["ingested_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    raw_load = warehouse.merge_batch("raw_events", validated, batch_id=batch_id, batch_date=batch_date)

    # ---- 4. features_daily through the shared transform --------------
    transformer = FeatureTransformer.from_contracts(contract)
    features = transformer.transform(validated)
    feature_load = warehouse.merge_batch("features_daily", features, batch_id=batch_id, batch_date=batch_date)

    # ---- 5. announce the batch ---------------------------------------
    landing = Storage.for_role("landing")
    gcs_uri = (
        batch_file_path
        if batch_file_path.startswith("gs://")
        else landing.uri(f"batches/date={batch_date}/{os.path.basename(batch_file_path)}")
    )
    event = build_event(
        "data.batch.landed",
        trace_id=trace_id,
        batch_id=batch_id,
        batch_date=batch_date,
        row_count=len(validated),
        gcs_uri=gcs_uri,
    )
    published = publisher.publish("data.batch.landed", event, trace_id=trace_id)

    duration_ms = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000.0
    log.info(
        f"Batch {batch_id} ingested: raw_events {raw_load['mode']}, features_daily {feature_load['mode']}",
        trace_id,
        batch_id=batch_id,
        latency_ms=round(duration_ms, 2),
        metrics={
            "rows_loaded": len(validated),
            "labelled_rows": labelled_rows,
            "raw_events_mode": raw_load["mode"],
        },
    )

    return {
        "status": "success",
        "batch_id": batch_id,
        "batch_date": batch_date,
        "trace_id": trace_id,
        "row_count": len(validated),
        "labelled_rows": labelled_rows,
        "raw_path": raw_load["path"],
        "features_path": feature_load["path"],
        "load_mode": raw_load["mode"],
        "quality": quality.as_dict(),
        "event_path": published.get("path"),
        "event_payload": event,
        "latency_ms": round(duration_ms, 2),
    }


def batch_path_from_cloudevent(payload: dict) -> str:
    """Resolve the object path from an Eventarc object-finalize CloudEvent."""
    data = payload.get("data", payload)
    bucket = data.get("bucket")
    name = data.get("name")
    if not bucket or not name:
        raise ValueError("CloudEvent payload has no bucket/name - not an object-finalize event")
    return f"gs://{bucket}/{name}"


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="ChurnOps ingest & validation job")
    parser.add_argument("batch_file", nargs="?", help="Path or gs:// URI of the landed batch")
    parser.add_argument("--cloudevent-file", help="JSON file holding the Eventarc object-finalize payload")
    parser.add_argument("--quarantine-dir", default=None)
    parser.add_argument("--warehouse-dir", default=None)
    parser.add_argument("--events-dir", default=None)
    args = parser.parse_args(argv)

    batch_file = args.batch_file
    if not batch_file and args.cloudevent_file:
        with open(args.cloudevent_file, "r", encoding="utf-8") as handle:
            batch_file = batch_path_from_cloudevent(json.load(handle))
    if not batch_file:
        batch_file = os.getenv("CHURNOPS_BATCH_URI", "")
    if not batch_file:
        parser.error("Provide a batch file, --cloudevent-file, or CHURNOPS_BATCH_URI")

    result = process_batch(
        batch_file_path=batch_file,
        quarantine_dir=args.quarantine_dir,
        warehouse_dir=args.warehouse_dir,
        events_dir=args.events_dir,
    )
    print(json.dumps(result, indent=2, default=str))
    if result["status"] == "quarantined":
        sys.exit(2)
    return result


if __name__ == "__main__":
    main()
