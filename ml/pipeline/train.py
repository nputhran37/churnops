"""
ChurnOps Model Training & Evaluation Gate Script (Person 2)
Trains Gradient Boosted classifier, computes business metrics (ROC-AUC, Recall@70P, Brier),
evaluates against champion, enforces quality gates, and exports model artifacts.
"""

import argparse
import datetime
import json
import logging
import os
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, precision_recall_curve, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
import yaml

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ml-train")


def emit_log(severity: str, message: str, trace_id: str, **kwargs):
    payload = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "severity": severity,
        "service": "ml-train",
        "message": message,
        "trace_id": trace_id,
        **kwargs,
    }
    logger.info(json.dumps(payload))


def calculate_recall_at_precision(y_true: np.ndarray, y_probs: np.ndarray, target_precision: float = 0.70) -> float:
    """Calculates recall at a fixed precision threshold (business metric for retention campaigns)."""
    precisions, recalls, thresholds = precision_recall_curve(y_true, y_probs)
    # Find points where precision >= target_precision
    valid_indices = np.where(precisions >= target_precision)[0]
    if len(valid_indices) == 0:
        return 0.0
    return float(np.round(np.max(recalls[valid_indices]), 4))


def train_and_gate_model(
    data_path: str,
    output_dir: str = "artifacts/models",
    gate_policy_path: str = "ml/pipeline/gate_policy.yaml",
    events_dir: str = "data/events_outbox",
    champion_auc: float = 0.810,
    trace_id: str | None = None,
    batch_id: str | None = None,
    force_fail: bool = False,
):
    with open(gate_policy_path, "r", encoding="utf-8") as f:
        policy = yaml.safe_load(f)["gates"]

    df = pd.read_parquet(data_path) if data_path.endswith(".parquet") else pd.read_csv(data_path)

    if not trace_id:
        trace_id = str(df["trace_id"].iloc[0]) if "trace_id" in df.columns else "tr-unknown"
    if not batch_id:
        batch_id = str(df["batch_id"].iloc[0]) if "batch_id" in df.columns else "b-unknown"

    emit_log("INFO", f"Starting training run for batch {batch_id} on {len(df)} rows", trace_id)

    # Define feature sets
    numeric_features = ["tenure", "monthly_charges", "total_charges"]
    categorical_features = [
        "gender",
        "partner",
        "dependents",
        "phone_service",
        "multiple_lines",
        "internet_service",
        "online_security",
        "online_backup",
        "device_protection",
        "tech_support",
        "streaming_tv",
        "streaming_movies",
        "contract",
        "paperless_billing",
        "payment_method",
    ]

    # Ensure required columns exist
    for col in numeric_features:
        if col not in df.columns:
            df[col] = 0.0
    for col in categorical_features:
        if col not in df.columns:
            df[col] = "Missing"

    X = df[numeric_features + categorical_features]
    y = df["churn"].values if "churn" in df.columns else np.random.choice([0, 1], size=len(df), p=[0.74, 0.26])

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)

    # Build preprocessing pipeline
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), numeric_features),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical_features),
        ]
    )

    if force_fail:
        # Deliberately degraded model for chaos testing
        emit_log("WARNING", "Force-fail mode enabled: creating degraded model to test gate rejection", trace_id)
        clf = LogisticRegression(C=1e-5, max_iter=50)
    else:
        clf = HistGradientBoostingClassifier(max_iter=100, random_state=42, min_samples_leaf=20)

    model_pipeline = Pipeline(steps=[("preprocessor", preprocessor), ("classifier", clf)])
    model_pipeline.fit(X_train, y_train)

    # Score challenger on holdout
    y_probs = model_pipeline.predict_proba(X_test)[:, 1]
    roc_auc = float(np.round(roc_auc_score(y_test, y_probs), 4))
    recall_70p = float(np.round(calculate_recall_at_precision(y_test, y_probs, 0.70), 4))
    brier = float(np.round(brier_score_loss(y_test, y_probs), 4))
    avg_pred_rate = float(np.round(np.mean(y_probs > 0.5), 4))

    metrics = {
        "roc_auc": roc_auc,
        "recall_at_70_precision": recall_70p,
        "brier_score": brier,
        "average_predicted_churn_rate": avg_pred_rate,
        "champion_roc_auc": champion_auc,
    }

    emit_log("INFO", f"Challenger evaluated: ROC-AUC={roc_auc}, Recall@70P={recall_70p}, Brier={brier}", trace_id, metrics=metrics)

    # 4. Enforce quality gates
    gate_passed = True
    rejection_reasons = []

    if roc_auc < policy["roc_auc"]["min_absolute"]:
        gate_passed = False
        rejection_reasons.append(f"ROC-AUC {roc_auc} below absolute threshold {policy['roc_auc']['min_absolute']}")

    if roc_auc < (champion_auc - policy["roc_auc"]["max_degradation_vs_champion"]):
        gate_passed = False
        rejection_reasons.append(
            f"Challenger ROC-AUC {roc_auc} degraded by more than {policy['roc_auc']['max_degradation_vs_champion']} vs champion {champion_auc}"
        )

    if brier > policy["brier_score"]["max_threshold"]:
        gate_passed = False
        rejection_reasons.append(f"Brier score {brier} exceeds limit {policy['brier_score']['max_threshold']}")

    if avg_pred_rate > policy["sanity_checks"]["max_churn_rate_prediction"]:
        gate_passed = False
        rejection_reasons.append(f"Predicted churn rate {avg_pred_rate} exceeds upper sanity limit")

    model_version = f"v-{datetime.date.today().strftime('%Y%m%d')}-{trace_id[-4:]}"
    model_dir = os.path.join(output_dir, model_version)
    os.makedirs(model_dir, exist_ok=True)

    model_path = os.path.join(model_dir, "model.joblib")
    meta_path = os.path.join(model_dir, "metadata.json")

    joblib.dump(model_pipeline, model_path)

    metadata = {
        "model_version": model_version,
        "trace_id": trace_id,
        "batch_id": batch_id,
        "git_sha": os.getenv("GIT_SHA", "dev-local"),
        "trained_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "metrics": metrics,
        "gate_passed": gate_passed,
        "rejection_reasons": rejection_reasons,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    os.makedirs(events_dir, exist_ok=True)

    if gate_passed:
        emit_log("INFO", f"QUALITY GATE PASSED! Model {model_version} promoted to challenger alias.", trace_id)
        event_payload = {
            "event_type": "model.trained",
            "schema_version": "1.0.0",
            "trace_id": trace_id,
            "batch_id": batch_id,
            "model_version": model_version,
            "model_artifact_uri": f"gs://proj-artifacts/models/{model_version}/model.joblib",
            "pipeline_run_id": f"pipe-{trace_id}",
            "git_sha": metadata["git_sha"],
            "metrics": {
                "roc_auc": roc_auc,
                "recall_at_70_precision": recall_70p,
                "brier_score": brier,
                "champion_roc_auc": champion_auc,
            },
            "emitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        event_file = os.path.join(events_dir, f"model_trained_{model_version}.json")
    else:
        emit_log("ERROR", f"QUALITY GATE FAILED! Model {model_version} rejected.", trace_id, reasons=rejection_reasons)
        event_payload = {
            "event_type": "model.rejected",
            "schema_version": "1.0.0",
            "trace_id": trace_id,
            "batch_id": batch_id,
            "rejected_model_version": model_version,
            "stage": "evaluation_gate",
            "reasons": rejection_reasons,
            "metrics": metrics,
            "emitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        event_file = os.path.join(events_dir, f"model_rejected_{model_version}.json")

    with open(event_file, "w", encoding="utf-8") as f:
        json.dump(event_payload, f, indent=2)

    return {
        "status": "gate_passed" if gate_passed else "gate_failed",
        "model_version": model_version,
        "metrics": metrics,
        "model_path": model_path,
        "metadata_path": meta_path,
        "event_path": event_file,
        "event_payload": event_payload,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChurnOps Model Training & Gating")
    parser.add_argument("data_file", help="Path to training parquet/csv")
    parser.add_argument("--output-dir", default="artifacts/models")
    parser.add_argument("--champion-auc", type=float, default=0.810)
    parser.add_argument("--trace-id", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--force-fail", action="store_true", help="Force failure for gate testing")

    args = parser.parse_args()
    res = train_and_gate_model(
        data_path=args.data_file,
        output_dir=args.output_dir,
        champion_auc=args.champion_auc,
        trace_id=args.trace_id,
        batch_id=args.batch_id,
        force_fail=args.force_fail,
    )
    print(json.dumps(res, indent=2, default=str))
