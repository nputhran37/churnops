"""
Bootstrap the ChurnOps data plane from the seed dataset (Person 1, Phase A).

`make seed` runs this once on a fresh clone or a fresh project. It:
  1. loads the seed extract (IBM Telco export if present, otherwise a synthetic
     stand-in drawn from the published Telco distributions),
  2. fits the generator's distribution profile and the transform statistics that
     contracts/features.yaml refers to,
  3. lands the seed in the landing bucket and loads it as raw_events partition zero,
  4. replays N days of stationary history with labels already released, so the
     warehouse looks like a platform that has been running for a month,
  5. freezes the reference window the drift detector compares against,
  6. materialises a labelled training snapshot for P2's pipeline.

Everything is idempotent: re-running replaces the same partitions.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "generator"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "drift"))

from common.contracts import load_feature_contract  # noqa: E402
from common.features import FeatureTransformer, load_feature_stats, save_feature_stats  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402
from common.storage import Storage  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from distributions import DistributionProfile  # noqa: E402
from generator import generate_baseline_batch  # noqa: E402
from reference import ReferenceWindow  # noqa: E402

DEFAULT_SEED_FILE = os.path.join("data", "seed", "telco_churn_seed.csv")
TRAINING_SNAPSHOT = os.path.join("data", "warehouse", "training_snapshot.parquet")
FEATURES_SQL = os.path.join("data", "ingest", "sql", "features_daily.sql")

log = get_logger("bootstrap")


# ----------------------------------------------------------------------
def load_or_synthesise_seed(seed_file: str, rows: int, seed_date: str) -> tuple[pd.DataFrame, str]:
    """Use the real Telco extract when it is checked out, else a faithful stand-in."""
    if os.path.exists(seed_file):
        frame = pd.read_csv(seed_file) if seed_file.endswith(".csv") else pd.read_parquet(seed_file)
        frame = normalise_telco_columns(frame)
        return frame, seed_file

    log.warning(
        f"No seed extract at {seed_file}; synthesising {rows} rows from the published Telco priors",
        "tr-bootstrap",
    )
    frame = generate_baseline_batch(
        n_rows=rows,
        batch_date=seed_date,
        batch_id="b-seed-000",
        trace_id="tr-seed-000",
        profile=DistributionProfile.builtin(),
        rng=np.random.default_rng(20260901),
    )
    return frame, "synthetic:telco-priors"


def normalise_telco_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map the Kaggle column names onto contracts/features.yaml names."""
    renames = {
        "customerID": "customer_id",
        "SeniorCitizen": "senior_citizen",
        "Partner": "partner",
        "Dependents": "dependents",
        "PhoneService": "phone_service",
        "MultipleLines": "multiple_lines",
        "InternetService": "internet_service",
        "OnlineSecurity": "online_security",
        "OnlineBackup": "online_backup",
        "DeviceProtection": "device_protection",
        "TechSupport": "tech_support",
        "StreamingTV": "streaming_tv",
        "StreamingMovies": "streaming_movies",
        "Contract": "contract",
        "PaperlessBilling": "paperless_billing",
        "PaymentMethod": "payment_method",
        "MonthlyCharges": "monthly_charges",
        "TotalCharges": "total_charges",
        "Churn": "churn",
        "gender": "gender",
        "tenure": "tenure",
    }
    out = df.rename(columns={k: v for k, v in renames.items() if k in df.columns}).copy()

    # TotalCharges ships as a string with blanks for brand-new customers.
    if "total_charges" in out.columns:
        out["total_charges"] = pd.to_numeric(out["total_charges"], errors="coerce")
        out["total_charges"] = out["total_charges"].fillna(out["monthly_charges"] * out.get("tenure", 0))
    if out.get("churn") is not None and out["churn"].dtype == object:
        out["churn"] = out["churn"].map({"Yes": 1, "No": 0}).fillna(0).astype(int)
    if "customer_id" in out.columns:
        out["customer_id"] = "CUST-" + out["customer_id"].astype(str).str.replace("-", "", regex=False).str.upper()
    if "customer_segment" not in out.columns:
        out["customer_segment"] = np.where(
            out["monthly_charges"] > 85.0, "Enterprise", np.where(out["monthly_charges"] > 50.0, "Premium", "Standard")
        )
    return out


def fit_transform_stats(df: pd.DataFrame, source: str) -> str:
    """Persist the mean/std/median/mode the shared transform and its SQL twin use."""
    contract = load_feature_contract()
    numeric = {}
    for name in contract.numeric_features:
        series = pd.to_numeric(df[name], errors="coerce").dropna()
        numeric[name] = {
            "mean": round(float(series.mean()), 4),
            "std": round(float(series.std() or 1.0), 4),
            "median": round(float(series.median()), 4),
        }
    categorical = {}
    for name in contract.categorical_features:
        mode = df[name].astype(str).mode()
        if len(mode):
            categorical[name] = {"mode": str(mode.iloc[0])}
    return save_feature_stats(numeric, source, categorical)


def write_features_sql(transformer: FeatureTransformer, path: str = FEATURES_SQL) -> str:
    """Emit the BigQuery twin of the Python transform for Dataform / scheduled queries."""
    warehouse = Warehouse.default()
    sql = transformer.to_sql(
        source_table=f"{warehouse.project}.{warehouse.dataset}.raw_events",
        dest_table=f"{warehouse.project}.{warehouse.dataset}.features_daily",
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(sql)
    return path


def replay_history(
    days: int,
    rows_per_day: int,
    end_date: datetime.date,
    profile: DistributionProfile,
    warehouse: Warehouse,
    transformer: FeatureTransformer,
) -> list[dict]:
    """Write `days` stationary, fully-labelled batches as if they had run daily."""
    loaded = []
    for offset in range(days, 0, -1):
        batch_date = (end_date - datetime.timedelta(days=offset)).isoformat()
        batch_id = f"b-{batch_date.replace('-', '')}-hist"
        trace_id = f"tr-hist-{batch_date.replace('-', '')}"
        frame = generate_baseline_batch(rows_per_day, batch_date, batch_id, trace_id, profile=profile)

        warehouse.merge_batch("raw_events", frame, batch_id=batch_id, batch_date=batch_date)
        warehouse.merge_batch(
            "features_daily", transformer.transform(frame), batch_id=batch_id, batch_date=batch_date
        )
        labels = frame[["customer_id", "churn", "batch_date", "batch_id", "trace_id"]].copy()
        labels["label_available_at"] = batch_date
        labels["released_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        warehouse.merge_batch("labels", labels, batch_id=batch_id, batch_date=batch_date)

        loaded.append({"batch_id": batch_id, "batch_date": batch_date, "rows": len(frame)})
    return loaded


def write_training_snapshot(warehouse: Warehouse, path: str = TRAINING_SNAPSHOT) -> dict:
    """Labelled raw_events history, the input P2's pipeline snapshots from."""
    frame = warehouse.read_table("raw_events")
    if frame.empty:
        return {"path": None, "rows": 0}

    labels = warehouse.read_table("labels")
    if not labels.empty:
        lookup = labels.drop_duplicates("customer_id").set_index("customer_id")["churn"]
        frame["churn"] = pd.to_numeric(frame.get("churn"), errors="coerce").fillna(
            pd.to_numeric(frame["customer_id"].map(lookup), errors="coerce")
        )

    labelled = frame[frame["churn"].notna()].copy()
    labelled["churn"] = labelled["churn"].astype(int)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    labelled.to_parquet(path, index=False)
    return {"path": path, "rows": len(labelled)}


# ----------------------------------------------------------------------
def bootstrap(
    seed_file: str = DEFAULT_SEED_FILE,
    seed_rows: int = 7000,
    history_days: int = 30,
    rows_per_day: int = 800,
    champion_version: str = "v-initial-champion",
    today: str | None = None,
) -> dict:
    today_date = datetime.date.fromisoformat(today) if today else datetime.date.today()
    seed_date = (today_date - datetime.timedelta(days=history_days + 1)).isoformat()

    warehouse = Warehouse.default()
    landing = Storage.for_role("landing")

    # 1. seed -------------------------------------------------------------
    seed_df, source = load_or_synthesise_seed(seed_file, seed_rows, seed_date)
    seed_df = seed_df.assign(
        batch_date=seed_date,
        batch_id="b-seed-000",
        trace_id="tr-seed-000",
        ingested_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    # 2. fitted artifacts --------------------------------------------------
    profile = DistributionProfile.fit(seed_df, source=source)
    profile_path = profile.save()
    stats_path = fit_transform_stats(seed_df, source)
    # Build the transformer from the stats we just fitted, not a cached copy.
    transformer = FeatureTransformer(contract=load_feature_contract(), stats=load_feature_stats())
    features_sql_path = write_features_sql(transformer)

    # 3. land the seed and load partition zero ------------------------------
    seed_object = "seed/telco_churn_seed.parquet"
    seed_path = landing.write_parquet(seed_object, seed_df)
    warehouse.merge_batch("raw_events", seed_df, batch_id="b-seed-000", batch_date=seed_date)
    warehouse.merge_batch(
        "features_daily", transformer.transform(seed_df), batch_id="b-seed-000", batch_date=seed_date
    )
    seed_labels = seed_df[["customer_id", "churn", "batch_date", "batch_id", "trace_id"]].copy()
    seed_labels["label_available_at"] = seed_date
    seed_labels["released_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    warehouse.merge_batch("labels", seed_labels, batch_id="b-seed-000", batch_date=seed_date)

    # 4. daily history ------------------------------------------------------
    history = replay_history(history_days, rows_per_day, today_date, profile, warehouse, transformer)

    # 5. reference window ---------------------------------------------------
    window = ReferenceWindow.build(
        frame=warehouse.read_table("raw_events"),
        model_version=champion_version,
        snapshot_date=(today_date - datetime.timedelta(days=1)).isoformat(),
        warehouse=warehouse,
        trace_id="tr-bootstrap",
    )

    # 6. training snapshot --------------------------------------------------
    snapshot = write_training_snapshot(warehouse)

    result = {
        "seed_source": source,
        "seed_rows": len(seed_df),
        "seed_object": landing.uri(seed_object),
        "seed_local_path": seed_path,
        "profile_path": profile_path,
        "feature_stats_path": stats_path,
        "features_sql_path": features_sql_path,
        "history_batches": len(history),
        "history_rows": sum(h["rows"] for h in history),
        "reference_window": {
            "model_version": window.model_version,
            "rows": window.row_count,
            "snapshot_date": window.snapshot_date,
            "sample_path": window.sample_path,
        },
        "training_snapshot": snapshot,
        "churn_base_rate": round(float(pd.to_numeric(seed_df["churn"], errors="coerce").mean()), 4),
    }

    log.info(
        "Data plane bootstrapped",
        "tr-bootstrap",
        metrics={
            "seed_rows": result["seed_rows"],
            "history_rows": result["history_rows"],
            "reference_rows": window.row_count,
            "training_rows": snapshot["rows"],
        },
    )
    return result


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Bootstrap the ChurnOps data plane")
    parser.add_argument("--seed-file", default=DEFAULT_SEED_FILE)
    parser.add_argument("--seed-rows", type=int, default=7000)
    parser.add_argument("--history-days", type=int, default=30)
    parser.add_argument("--rows-per-day", type=int, default=800)
    parser.add_argument("--champion-version", default="v-initial-champion")
    parser.add_argument("--today", default=None, help="Pin 'today' for reproducible bootstraps")
    args = parser.parse_args(argv)

    result = bootstrap(
        seed_file=args.seed_file,
        seed_rows=args.seed_rows,
        history_days=args.history_days,
        rows_per_day=args.rows_per_day,
        champion_version=args.champion_version,
        today=args.today,
    )
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
