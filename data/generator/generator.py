"""
ChurnOps synthetic batch generator with drift injection (Person 1, Phase B).

Runs as a Cloud Run Job once a day (Cloud Scheduler, wired by P3) and on demand via
`make inject-drift SCENARIO=price_hike SEVERITY=0.8`.

What it does on each run:
  1. samples N customer-activity rows from the profile fitted on the seed dataset
     (per-column marginals plus charges|internet_service and tenure|contract),
  2. applies the named drift scenario from scenarios.yaml at the given severity,
  3. writes a Parquet batch to gs://<project>-landing/batches/date=YYYY-MM-DD/,
  4. withholds the churn labels and releases the labels of the batch generated
     LABEL_DELAY_DAYS ago, which is why drift has to be the early-warning signal.

Nothing here publishes events: the ingest job owns data.batch.landed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid
import zlib

import numpy as np
import pandas as pd

# Allow `python data/generator/generator.py` from the repo root and `COPY data/common ./common`
# inside the container to resolve the shared library the same way.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.contracts import load_feature_contract  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402
from common.storage import Storage  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from distributions import DistributionProfile, truncated_normal  # noqa: E402
from scenarios import ScenarioCatalogue, apply_scenario  # noqa: E402

LABEL_DELAY_DAYS = int(os.getenv("CHURNOPS_LABEL_DELAY_DAYS", "14"))
DEFAULT_BATCH_SIZE = int(os.getenv("CHURNOPS_BATCH_SIZE", "2000"))

log = get_logger("generator")


# ----------------------------------------------------------------------
# sampling
# ----------------------------------------------------------------------
def _seed_from(batch_date: str, batch_id: str) -> int:
    """Deterministic per-batch seed (hash() is salted per process, crc32 is not)."""
    return zlib.crc32(f"{batch_date}|{batch_id}".encode())


def generate_baseline_batch(
    n_rows: int,
    batch_date: str,
    batch_id: str,
    trace_id: str,
    profile: DistributionProfile | None = None,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Sample a stationary batch from the fitted reference distribution."""
    profile = profile or DistributionProfile.load()
    rng = rng or np.random.default_rng(_seed_from(batch_date, batch_id))
    contract = load_feature_contract()

    frame: dict[str, object] = {
        "customer_id": [f"CUST-{uuid.uuid4().hex[:8].upper()}" for _ in range(n_rows)]
    }

    # Independent categorical marginals.
    independent = [
        "gender",
        "senior_citizen",
        "partner",
        "dependents",
        "phone_service",
        "internet_service",
        "contract",
        "paperless_billing",
        "payment_method",
    ]
    for name in independent:
        values, probabilities = profile.categories(name)
        frame[name] = rng.choice(values, size=n_rows, p=probabilities)

    df = pd.DataFrame(frame)
    df["senior_citizen"] = pd.to_numeric(df["senior_citizen"], errors="coerce").fillna(0).astype("int64")

    # Service columns are logically dependent on phone/internet availability.
    df["multiple_lines"] = _dependent_category(df, profile, "multiple_lines", "phone_service", "No phone service", rng)
    for name in ("online_security", "online_backup", "device_protection", "tech_support", "streaming_tv", "streaming_movies"):
        df[name] = _dependent_category(df, profile, name, "internet_service", "No internet service", rng)

    # tenure | contract  (long contracts hold older customers)
    tenure_spec = contract.get("tenure")
    tenure = np.zeros(n_rows)
    for level in df["contract"].unique():
        mask = (df["contract"] == level).to_numpy()
        stats = profile.conditional_stats("tenure|contract", str(level), "tenure")
        tenure[mask] = truncated_normal(
            rng,
            stats.get("mean", 32.0),
            stats.get("std", 24.0),
            float(tenure_spec.minimum or 0) + 1,
            float(tenure_spec.maximum or 72),
            int(mask.sum()),
        )
    df["tenure"] = np.rint(tenure).astype("int64")

    # monthly_charges | internet_service
    charges_spec = contract.get("monthly_charges")
    charges = np.zeros(n_rows)
    for level in df["internet_service"].unique():
        mask = (df["internet_service"] == level).to_numpy()
        stats = profile.conditional_stats("monthly_charges|internet_service", str(level), "monthly_charges")
        charges[mask] = truncated_normal(
            rng,
            stats.get("mean", 65.0),
            stats.get("std", 30.0),
            float(charges_spec.minimum or 10.0) + 8.0,
            float(profile.numeric.get("monthly_charges", {}).get("max", 118.75)),
            int(mask.sum()),
        )
    df["monthly_charges"] = np.round(charges, 2)

    total_spec = contract.get("total_charges")
    df["total_charges"] = np.round(
        np.clip(
            df["monthly_charges"] * df["tenure"] * rng.uniform(0.95, 1.05, size=n_rows),
            float(total_spec.minimum or 0.0),
            float(total_spec.maximum or 15000.0),
        ),
        2,
    )

    bounds = profile.segment_bounds
    df["customer_segment"] = np.where(
        df["monthly_charges"] > bounds.get("enterprise", 85.0),
        "Enterprise",
        np.where(df["monthly_charges"] > bounds.get("premium", 50.0), "Premium", "Standard"),
    )

    df["churn"] = (rng.uniform(0.0, 1.0, size=n_rows) < profile.churn_probability(df)).astype("int64")

    df["batch_date"] = batch_date
    df["batch_id"] = batch_id
    df["trace_id"] = trace_id
    df["ingested_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    ordered = [c for c in contract.raw_columns if c in df.columns]
    remaining = [c for c in df.columns if c not in ordered]
    return df[ordered + remaining]


def _dependent_category(
    df: pd.DataFrame,
    profile: DistributionProfile,
    name: str,
    parent: str,
    unavailable_label: str,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample a service column, respecting 'No phone/internet service' consistency."""
    values, probabilities = profile.categories(name)
    pairs = [(v, p) for v, p in zip(values, probabilities, strict=True) if v != unavailable_label]
    total = sum(p for _, p in pairs) or 1.0
    options = [v for v, _ in pairs]
    weights = [p / total for _, p in pairs]

    sampled = rng.choice(options, size=len(df), p=weights)
    return np.where(df[parent].astype(str) == "No", unavailable_label, sampled)


# ----------------------------------------------------------------------
# delayed labels
# ----------------------------------------------------------------------
def _pending_label_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "labels", "_pending")


def stage_pending_labels(df: pd.DataFrame, batch_id: str, batch_date: str, output_dir: str) -> str:
    """Park the true labels; they are only released LABEL_DELAY_DAYS later."""
    pending_dir = _pending_label_dir(output_dir)
    os.makedirs(pending_dir, exist_ok=True)
    path = os.path.join(pending_dir, f"{batch_date}__{batch_id}.parquet")
    labels = df[["customer_id", "churn", "batch_date", "batch_id", "trace_id"]].copy()
    labels["label_available_at"] = (
        datetime.date.fromisoformat(batch_date) + datetime.timedelta(days=LABEL_DELAY_DAYS)
    ).isoformat()
    labels.to_parquet(path, index=False)
    return path


def release_due_labels(as_of: str, output_dir: str, warehouse: Warehouse | None = None) -> list[dict]:
    """Move every pending label set whose delay has elapsed into the labels table."""
    warehouse = warehouse or Warehouse.default()
    pending_dir = _pending_label_dir(output_dir)
    if not os.path.isdir(pending_dir):
        return []

    released: list[dict] = []
    as_of_date = datetime.date.fromisoformat(as_of)
    for entry in sorted(os.listdir(pending_dir)):
        if not entry.endswith(".parquet"):
            continue
        batch_date = entry.split("__", 1)[0]
        try:
            available_at = datetime.date.fromisoformat(batch_date) + datetime.timedelta(days=LABEL_DELAY_DAYS)
        except ValueError:
            continue
        if available_at > as_of_date:
            continue

        path = os.path.join(pending_dir, entry)
        labels = pd.read_parquet(path)
        batch_id = str(labels["batch_id"].iloc[0])
        labels = labels.assign(released_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        warehouse.merge_batch("labels", labels, batch_id=batch_id, batch_date=batch_date)
        os.remove(path)
        released.append({"batch_id": batch_id, "batch_date": batch_date, "rows": len(labels)})

    return released


# ----------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------
def run_generator(
    scenario: str = "none",
    severity: float = 0.0,
    date_str: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    output_dir: str = os.path.join("data", "landing"),
    trace_id: str | None = None,
    withhold_labels: bool = True,
    catalogue: ScenarioCatalogue | None = None,
) -> dict:
    date_str = date_str or datetime.date.today().isoformat()
    trace_id = trace_id or f"tr-{uuid.uuid4().hex[:12]}"
    batch_id = f"b-{date_str.replace('-', '')}-{uuid.uuid4().hex[:6]}"

    catalogue = catalogue or ScenarioCatalogue.load()
    spec = catalogue.get(scenario)
    profile = DistributionProfile.load()
    rng = np.random.default_rng(_seed_from(date_str, batch_id))

    log.info(
        f"Generating batch for {date_str} (scenario={scenario}, severity={severity})",
        trace_id,
        batch_id=batch_id,
        scenario=scenario,
        severity=severity,
        row_count=batch_size,
        profile_source=profile.fitted_from,
    )

    baseline = generate_baseline_batch(batch_size, date_str, batch_id, trace_id, profile=profile, rng=rng)
    drifted, audit = apply_scenario(baseline, spec, severity, rng=rng)

    for step in audit:
        log.warning(
            f"Injected {step['operation']} on {step['feature']}",
            trace_id,
            batch_id=batch_id,
            scenario=scenario,
            metrics=step,
        )

    labels_path = stage_pending_labels(drifted, batch_id, date_str, output_dir)
    landed = drifted.copy()
    if withhold_labels:
        landed["churn"] = pd.Series([pd.NA] * len(landed), dtype="Int64")

    storage = Storage.for_role("landing", local_root=output_dir)
    object_path = f"batches/date={date_str}/batch_{batch_id}.parquet"
    parquet_path = storage.write_parquet(object_path, landed)
    csv_path = parquet_path.replace(".parquet", ".csv") if parquet_path.endswith(".parquet") else parquet_path
    if not storage.remote:
        landed.to_csv(csv_path, index=False)

    released = release_due_labels(date_str, output_dir)
    if released:
        log.info(
            f"Released delayed labels for {len(released)} batch(es)",
            trace_id,
            batch_id=batch_id,
            released=released,
        )

    churn_rate = float(drifted["churn"].mean())
    log.info(
        f"Batch {batch_id} written with {len(landed)} rows",
        trace_id,
        batch_id=batch_id,
        parquet_path=parquet_path,
        gcs_uri=storage.uri(object_path),
        true_churn_rate=round(churn_rate, 4),
        labels_available_on=(
            datetime.date.fromisoformat(date_str) + datetime.timedelta(days=LABEL_DELAY_DAYS)
        ).isoformat(),
    )

    return {
        "batch_id": batch_id,
        "batch_date": date_str,
        "trace_id": trace_id,
        "row_count": len(landed),
        "scenario": scenario,
        "severity": severity,
        "expected_drift": spec.expected_drift,
        "expect_quarantine": spec.expect_quarantine,
        "business_narrative": spec.business_narrative,
        "parquet_path": parquet_path,
        "csv_path": csv_path,
        "gcs_uri": storage.uri(object_path),
        "pending_labels_path": labels_path,
        "released_labels": released,
        "true_churn_rate": round(churn_rate, 4),
        "drift_audit": audit,
    }


def main(argv: list[str] | None = None) -> dict:
    catalogue = ScenarioCatalogue.load()
    parser = argparse.ArgumentParser(description="ChurnOps synthetic batch generator")
    parser.add_argument("--scenario", default="none", choices=catalogue.names)
    parser.add_argument("--severity", type=float, default=0.0, help="0-1 knob scaling the scenario")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output-dir", default=os.path.join("data", "landing"))
    parser.add_argument("--trace-id", default=None)
    parser.add_argument(
        "--include-labels",
        action="store_true",
        help="Land the churn labels with the batch instead of delaying them (backfills only)",
    )
    args = parser.parse_args(argv)

    result = run_generator(
        scenario=args.scenario,
        severity=args.severity,
        date_str=args.date,
        batch_size=args.batch_size,
        output_dir=args.output_dir,
        trace_id=args.trace_id,
        withhold_labels=not args.include_labels,
        catalogue=catalogue,
    )
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
