"""
Realised performance backfill (Person 1, Phase D step 7).

Drift alerts are only worth trusting if they really did predict degradation. Two
simulated weeks after a batch lands its labels arrive; this job scores the champion
on those labelled rows and writes the realised ROC-AUC back onto the batch's
drift_reports row. Plotting realised_auc against severity is the evidence that the
early-warning signal works - and it is what P2's Drift Analyst quotes in the
closing report.

Runs daily as a Cloud Run Job (Cloud Scheduler) or via `make realised-performance`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.contracts import load_feature_contract  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from prediction import _offline_scorer  # noqa: E402

log = get_logger("drift-detector")


def _roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """ROC-AUC via the rank statistic, so the drift image needs no scikit-learn."""
    if y_true.size == 0 or len(np.unique(y_true)) < 2:
        return None
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, scores.size + 1)

    # average ranks within ties
    frame = pd.DataFrame({"score": scores, "rank": ranks})
    ranks = frame.groupby("score")["rank"].transform("mean").to_numpy()

    positives = y_true == 1
    n_pos, n_neg = int(positives.sum()), int((~positives).sum())
    auc = (ranks[positives].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(np.round(auc, 4))


def backfill_batch(batch_id: str, warehouse: Warehouse, contract=None) -> dict:
    """Compute the champion's realised AUC on one batch, once its labels exist."""
    contract = contract or load_feature_contract()

    events = warehouse.read_table("raw_events")
    if events.empty:
        return {"batch_id": batch_id, "status": "skipped", "reason": "raw_events empty"}
    batch = events[events["batch_id"].astype(str) == batch_id]
    if batch.empty:
        return {"batch_id": batch_id, "status": "skipped", "reason": "batch not in raw_events"}

    labels = warehouse.read_table("labels")
    if labels.empty:
        return {"batch_id": batch_id, "status": "waiting", "reason": "no labels released yet"}

    batch_labels = labels[labels["batch_id"].astype(str) == batch_id]
    if batch_labels.empty:
        return {"batch_id": batch_id, "status": "waiting", "reason": "labels for this batch have not arrived"}

    merged = batch.merge(
        batch_labels[["customer_id", "churn"]].rename(columns={"churn": "observed_churn"}),
        on="customer_id",
        how="inner",
    )
    if merged.empty:
        return {"batch_id": batch_id, "status": "waiting", "reason": "no overlapping customers"}

    scores = _offline_scorer(merged, contract)
    observed = pd.to_numeric(merged["observed_churn"], errors="coerce").fillna(0).astype(int).to_numpy()
    auc = _roc_auc(observed, np.asarray(scores, dtype=float))
    if auc is None:
        return {"batch_id": batch_id, "status": "skipped", "reason": "labels are single-class"}

    updated = warehouse.update_row(
        "drift_reports",
        keys={"batch_id": batch_id},
        updates={
            "realised_auc": auc,
            "realised_label_count": int(len(merged)),
            "realised_auc_computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )
    return {
        "batch_id": batch_id,
        "status": "updated" if updated else "no_report_row",
        "realised_auc": auc,
        "labelled_rows": int(len(merged)),
        "observed_churn_rate": round(float(observed.mean()), 4),
    }


def backfill_all(warehouse_dir: str | None = None, only_missing: bool = True) -> dict:
    """Backfill every drift report whose labels have arrived."""
    warehouse = Warehouse.default(root=warehouse_dir)
    contract = load_feature_contract()
    reports = warehouse.read_rows("drift_reports")

    results = []
    for report in reports:
        if only_missing and report.get("realised_auc") is not None:
            continue
        results.append(backfill_batch(str(report["batch_id"]), warehouse, contract))

    updated = [r for r in results if r["status"] == "updated"]
    if updated:
        log.info(
            f"Backfilled realised performance for {len(updated)} drift report(s)",
            "tr-realised-performance",
            metrics={
                "updated": len(updated),
                "mean_realised_auc": round(float(np.mean([r["realised_auc"] for r in updated])), 4),
            },
        )
    else:
        log.info("No drift reports had newly available labels", "tr-realised-performance")

    return {"checked": len(results), "updated": len(updated), "details": results}


def degradation_evidence(warehouse_dir: str | None = None) -> dict:
    """Mean realised AUC split by the severity the detector called at the time."""
    warehouse = Warehouse.default(root=warehouse_dir)
    rows = [r for r in warehouse.read_rows("drift_reports") if r.get("realised_auc") is not None]
    if not rows:
        return {"samples": 0}

    frame = pd.DataFrame(rows)
    grouped = frame.groupby("severity")["realised_auc"].agg(["mean", "count"]).round(4)
    return {
        "samples": len(frame),
        "by_severity": {str(k): {"mean_realised_auc": float(v["mean"]), "batches": int(v["count"])}
                        for k, v in grouped.to_dict("index").items()},
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Backfill realised champion performance from delayed labels")
    parser.add_argument("--batch-id", default=None, help="Backfill a single batch")
    parser.add_argument("--warehouse-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Recompute even where realised_auc is already set")
    args = parser.parse_args(argv)

    if args.batch_id:
        result = backfill_batch(args.batch_id, Warehouse.default(root=args.warehouse_dir))
    else:
        result = backfill_all(warehouse_dir=args.warehouse_dir, only_missing=not args.all)
        result["evidence"] = degradation_evidence(args.warehouse_dir)

    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
