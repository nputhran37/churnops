"""
Reference window management (Person 1, Phase D).

The reference window is the data the current champion was trained on. Drift is
always measured against it, and it is refreshed - never silently - when
model.promoted fires, so "drifted" keeps meaning "different from what the live
model learned".

Two representations are kept in step:
  * a sampled frame on GCS, used for the distributional tests (PSI/KS/JS/chi2),
  * one row per feature in the reference_window BigQuery table
    (contracts/bq/reference_window.json), which is what dashboards and the LLM read.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
from common.contracts import FeatureContract, load_feature_contract
from common.logging_utils import get_logger
from common.warehouse import Warehouse

REFERENCE_SAMPLE_FILENAME = "reference_sample.parquet"
MAX_SAMPLE_ROWS = int(os.getenv("CHURNOPS_REFERENCE_SAMPLE_ROWS", "5000"))

log = get_logger("drift-detector")


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class ReferenceWindow:
    frame: pd.DataFrame
    model_version: str
    snapshot_date: str
    sample_path: str

    @property
    def row_count(self) -> int:
        return len(self.frame)

    # ------------------------------------------------------------------
    @staticmethod
    def sample_path_for(warehouse: Warehouse) -> str:
        return os.path.join(warehouse.root, "reference_window", REFERENCE_SAMPLE_FILENAME)

    @classmethod
    def build(
        cls,
        frame: pd.DataFrame,
        model_version: str,
        snapshot_date: str | None = None,
        warehouse: Warehouse | None = None,
        contract: FeatureContract | None = None,
        trace_id: str = "tr-reference",
    ) -> ReferenceWindow:
        """Freeze a new reference window and publish its per-feature statistics."""
        warehouse = warehouse or Warehouse.default()
        contract = contract or load_feature_contract()
        snapshot_date = snapshot_date or datetime.date.today().isoformat()

        sample = frame if len(frame) <= MAX_SAMPLE_ROWS else frame.sample(MAX_SAMPLE_ROWS, random_state=7)
        sample_path = cls.sample_path_for(warehouse)
        os.makedirs(os.path.dirname(sample_path), exist_ok=True)
        sample.to_parquet(sample_path, index=False)

        rows = summarise_features(sample, model_version, snapshot_date, contract)
        warehouse.replace_table("reference_window", rows)

        log.info(
            f"Reference window refreshed for champion {model_version}",
            trace_id,
            model_version=model_version,
            metrics={"rows": len(sample), "features": len(rows), "snapshot_date": snapshot_date},
        )
        return cls(frame=sample, model_version=model_version, snapshot_date=snapshot_date, sample_path=sample_path)

    @classmethod
    def load(
        cls,
        warehouse: Warehouse | None = None,
        sample_path: str | None = None,
    ) -> ReferenceWindow | None:
        """Load the frozen reference window, or None when the platform is cold."""
        warehouse = warehouse or Warehouse.default()
        path = sample_path or cls.sample_path_for(warehouse)
        if not os.path.exists(path):
            return None

        rows = warehouse.read_rows("reference_window")
        model_version = rows[0]["model_version"] if rows else "unknown"
        snapshot_date = rows[0]["snapshot_date"] if rows else datetime.date.today().isoformat()
        return cls(
            frame=pd.read_parquet(path),
            model_version=str(model_version),
            snapshot_date=str(snapshot_date),
            sample_path=path,
        )


def summarise_features(
    df: pd.DataFrame,
    model_version: str,
    snapshot_date: str,
    contract: FeatureContract | None = None,
) -> list[dict]:
    """One reference_window row per contract feature."""
    contract = contract or load_feature_contract()
    now = _utc_now()
    rows: list[dict] = []

    for spec in contract.features:
        if spec.name not in df.columns:
            continue
        base = {
            "model_version": model_version,
            "feature_name": spec.name,
            "dtype": spec.dtype,
            "sample_count": int(df[spec.name].notna().sum()),
            "snapshot_date": snapshot_date,
            "updated_at": now,
        }
        if spec.is_numeric:
            series = pd.to_numeric(df[spec.name], errors="coerce").dropna()
            base.update(
                {
                    "mean": float(series.mean()) if len(series) else None,
                    "std": float(series.std()) if len(series) else None,
                    "quantiles": json.dumps(
                        {
                            "q10": _q(series, 0.10),
                            "q25": _q(series, 0.25),
                            "q50": _q(series, 0.50),
                            "q75": _q(series, 0.75),
                            "q90": _q(series, 0.90),
                        }
                    ),
                    "category_distribution": None,
                }
            )
        else:
            distribution = df[spec.name].astype(str).value_counts(normalize=True).round(6).to_dict()
            base.update(
                {
                    "mean": None,
                    "std": None,
                    "quantiles": None,
                    "category_distribution": json.dumps(distribution),
                }
            )
        rows.append(base)

    return rows


def _q(series: pd.Series, quantile: float) -> float | None:
    if series.empty:
        return None
    return round(float(series.quantile(quantile)), 4)


def build_from_warehouse(
    warehouse: Warehouse | None = None,
    model_version: str = "unknown",
    up_to_date: str | None = None,
    lookback_partitions: int = 30,
    trace_id: str = "tr-reference",
) -> ReferenceWindow | None:
    """Rebuild the window from the raw_events partitions the champion was trained on."""
    warehouse = warehouse or Warehouse.default()
    frame = warehouse.read_table("raw_events", limit_partitions=lookback_partitions)
    if frame.empty:
        log.warning("Cannot refresh reference window: raw_events is empty", trace_id)
        return None

    if up_to_date and "batch_date" in frame.columns:
        frame = frame[frame["batch_date"].astype(str) <= up_to_date]
    if frame.empty:
        return None

    return ReferenceWindow.build(
        frame=frame,
        model_version=model_version,
        snapshot_date=up_to_date or datetime.date.today().isoformat(),
        warehouse=warehouse,
        trace_id=trace_id,
    )


def refresh_on_model_promoted(event: dict, warehouse: Warehouse | None = None) -> dict:
    """Handle a model.promoted event: the new champion defines the new reference."""
    warehouse = warehouse or Warehouse.default()
    model_version = str(event.get("promoted_model_version", "unknown"))
    trace_id = str(event.get("trace_id", "tr-unset"))

    window = build_from_warehouse(
        warehouse=warehouse,
        model_version=model_version,
        up_to_date=event.get("snapshot_date"),
        trace_id=trace_id,
    )
    if window is None:
        return {"status": "skipped", "reason": "no raw_events to build a reference window from"}

    return {
        "status": "refreshed",
        "model_version": model_version,
        "rows": window.row_count,
        "snapshot_date": window.snapshot_date,
        "sample_path": window.sample_path,
    }


def psi_bucket_edges(series: pd.Series, buckets: int = 10) -> np.ndarray:
    """Quantile edges of the reference distribution, reused for every batch."""
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy()
    if values.size == 0:
        return np.array([-np.inf, np.inf])
    edges = np.unique(np.percentile(values, np.linspace(0, 100, buckets + 1)))
    edges[0] = -np.inf
    edges[-1] = np.inf
    return edges
