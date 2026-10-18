"""KFP pipeline: train on labelled BigQuery history and publish a gated result."""

import os

from kfp import compiler, dsl
from kfp.dsl import Metrics, Model, Output, component


@component(
    base_image="python:3.11-slim",
    packages_to_install=[
        "google-cloud-bigquery>=3.20",
        "google-cloud-storage>=2.16",
        "google-cloud-pubsub>=2.21",
        "db-dtypes>=1.2",
        "pandas>=2.1",
        "numpy>=1.26",
        "scikit-learn>=1.3",
        "joblib>=1.3",
    ],
)
def train_and_gate(
    project_id: str,
    dataset_id: str,
    snapshot_date: str,
    drift_trace_id: str,
    trigger_reason: str,
    champion_auc: float,
    model: Output[Model],
    metrics: Output[Metrics],
):
    import datetime
    import json
    import os

    import joblib
    import numpy as np
    import pandas as pd
    from google.cloud import bigquery, pubsub_v1, storage
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import brier_score_loss, precision_recall_curve, roc_auc_score
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    query = f"""
      SELECT * FROM `{project_id}.{dataset_id}.raw_events`
      WHERE batch_date <= @snapshot_date AND churn IS NOT NULL
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("snapshot_date", "DATE", snapshot_date)
    ])
    frame = bigquery.Client(project=project_id).query(query, job_config=job_config).to_dataframe()
    if len(frame) < 40 or frame["churn"].nunique() != 2:
        raise ValueError(f"Need at least 40 labelled examples with both classes; got {len(frame)}")

    numeric = ["tenure", "monthly_charges", "total_charges"]
    categorical = [
        "gender", "partner", "dependents", "phone_service", "multiple_lines",
        "internet_service", "online_security", "online_backup", "device_protection",
        "tech_support", "streaming_tv", "streaming_movies", "contract",
        "paperless_billing", "payment_method",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    for column in categorical:
        frame[column] = frame[column].fillna("Missing").astype(str)
    x = frame[numeric + categorical]
    y = frame["churn"].astype(int)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.25, random_state=42, stratify=y
    )
    pipeline = Pipeline([
        ("features", ColumnTransformer([
            ("num", StandardScaler(), numeric),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical),
        ])),
        ("classifier", HistGradientBoostingClassifier(max_iter=100, random_state=42, min_samples_leaf=10)),
    ])
    pipeline.fit(x_train, y_train)
    probabilities = pipeline.predict_proba(x_test)[:, 1]
    auc = float(roc_auc_score(y_test, probabilities))
    brier = float(brier_score_loss(y_test, probabilities))
    precisions, recalls, _ = precision_recall_curve(y_test, probabilities)
    recall_at_70p = float(np.max(recalls[precisions >= 0.70])) if np.any(precisions >= 0.70) else 0.0
    passed = auc >= max(0.75, champion_auc - 0.005) and brier <= 0.20

    version = f"v-{datetime.datetime.now(datetime.timezone.utc):%Y%m%d}-{drift_trace_id[-6:]}"
    output_dir = "/tmp/churnops-model"
    os.makedirs(output_dir, exist_ok=True)
    model_file = os.path.join(output_dir, "model.joblib")
    joblib.dump(pipeline, model_file)
    joblib.dump(pipeline, model.path)
    result_metrics = {
        "roc_auc": round(auc, 4),
        "recall_at_70_precision": round(recall_at_70p, 4),
        "brier_score": round(brier, 4),
        "champion_roc_auc": champion_auc,
        "gate_passed": passed,
        "training_rows": int(len(frame)),
    }
    bucket_name = f"{project_id}-artifacts"
    prefix = f"models/{version}"
    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)
    bucket.blob(f"{prefix}/model.joblib").upload_from_filename(model_file)
    model_uri = f"gs://{bucket_name}/{prefix}/model.joblib"
    model.uri = model_uri
    model.metadata.update(result_metrics)
    model.metadata["model_version"] = version
    for key, value in result_metrics.items():
        if isinstance(value, (int, float)):
            metrics.log_metric(key, float(value))

    publisher = pubsub_v1.PublisherClient()
    event = {
        "schema_version": "1.0.0",
        "trace_id": drift_trace_id,
        "batch_id": str(frame["batch_id"].iloc[-1]),
        "emitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "metrics": {k: result_metrics[k] for k in (
            "roc_auc", "recall_at_70_precision", "brier_score", "champion_roc_auc"
        )},
    }
    if passed:
        event.update({"event_type": "model.trained", "model_version": version,
                      "model_artifact_uri": model_uri, "pipeline_run_id": f"vertex-{drift_trace_id}",
                      "git_sha": os.getenv("GIT_SHA", "unknown")})
        topic = "model-trained"
    else:
        event.update({"event_type": "model.rejected", "rejected_model_version": version,
                      "stage": "evaluation_gate", "reasons": [
                          f"ROC-AUC={auc:.4f}; Brier={brier:.4f}; quality gate failed"
                      ]})
        topic = "model-rejected"
    publisher.publish(publisher.topic_path(project_id, topic), json.dumps(event).encode()).result(timeout=30)
    print(json.dumps({"trigger_reason": trigger_reason, "event": event, "gate_passed": passed}))


@dsl.pipeline(
    name="churnops-continuous-training-pipeline",
    description="Train and gate a challenger using labelled ChurnOps history.",
)
def churn_training_pipeline(
    snapshot_date: str = "2026-10-19",
    drift_trace_id: str = "tr-default",
    trigger_reason: str = "drift.detected",
    project_id: str = "churnops-512010",
    dataset_id: str = "churnops",
    champion_auc: float = 0.810,
):
    training_task = train_and_gate(
        project_id=project_id,
        dataset_id=dataset_id,
        snapshot_date=snapshot_date,
        drift_trace_id=drift_trace_id,
        trigger_reason=trigger_reason,
        champion_auc=champion_auc,
    )
    # The demo GCP project currently has a one-vCPU Vertex custom-training
    # quota. The tabular sample is small enough for a single CPU and 4 GiB.
    training_task.set_cpu_limit("1")
    training_task.set_memory_limit("4Gi")


if __name__ == "__main__":
    output_yaml = os.getenv("PIPELINE_SPEC", "ml/pipeline/churn_pipeline.yaml")
    os.makedirs(os.path.dirname(output_yaml), exist_ok=True)
    compiler.Compiler().compile(pipeline_func=churn_training_pipeline, package_path=output_yaml)
    print(f"Compiled Vertex pipeline: {output_yaml}")
