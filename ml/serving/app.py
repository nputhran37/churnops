"""
ChurnOps FastAPI Prediction Service (Person 2)
High performance, scale-to-zero inference API with p95 < 300ms latency.
"""

import datetime
import hashlib
import json
import logging
import os
import time
import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from schemas import HealthResponse, PredictRequest, PredictResponse, SinglePredictionResult, VersionResponse

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("serving")

app = FastAPI(title="ChurnOps Prediction Service", version="1.0.0")

# Global state
MODEL_PIPELINE = None
MODEL_VERSION = os.getenv("MODEL_VERSION", "v-initial-champion")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "champion")
GIT_SHA = os.getenv("GIT_SHA", "dev-local")
SERVICE_VERSION = "1.0.0"
LOG_DIR = os.getenv("LOG_DIR", "data/predictions_log")


def emit_log(severity: str, message: str, trace_id: str = "tr-serving", **kwargs):
    payload = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "severity": severity,
        "service": "serving",
        "message": message,
        "trace_id": trace_id,
        "model_version": MODEL_VERSION,
        **kwargs,
    }
    logger.info(json.dumps(payload))


def load_model():
    global MODEL_PIPELINE, MODEL_VERSION
    model_path = os.getenv("MODEL_PATH", "artifacts/models/champion/model.joblib")
    if os.path.exists(model_path):
        try:
            MODEL_PIPELINE = joblib.load(model_path)
            emit_log("INFO", f"Model successfully loaded from {model_path}")
            return
        except Exception as e:
            emit_log("ERROR", f"Error loading model from {model_path}: {e}")

    # Fallback to in-memory lightweight heuristic classifier for walking skeleton
    emit_log("WARNING", "Artifact model not found. Using baseline fallback classifier for Walking Skeleton.")
    MODEL_PIPELINE = "BASELINE_HEURISTIC"


@app.on_event("startup")
def startup_event():
    load_model()
    os.makedirs(LOG_DIR, exist_ok=True)


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        model_loaded=MODEL_PIPELINE is not None,
        model_version=MODEL_VERSION,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


@app.get("/version", response_model=VersionResponse)
def version():
    return VersionResponse(
        model_version=MODEL_VERSION,
        model_alias=MODEL_ALIAS,
        git_sha=GIT_SHA,
        service_version=SERVICE_VERSION,
    )


@app.post("/predict", response_model=PredictResponse)
def predict(request: PredictRequest):
    start_time = time.perf_counter()
    trace_id = request.trace_id or f"tr-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    if not request.instances:
        raise HTTPException(status_code=400, detail="Instances list cannot be empty")

    results = []
    log_entries = []

    # Prepare DataFrame
    records = [inst.model_dump() for inst in request.instances]
    df_instances = pd.DataFrame(records)

    if MODEL_PIPELINE == "BASELINE_HEURISTIC" or MODEL_PIPELINE is None:
        # Realistic logistic approximation for walking skeleton
        for record in records:
            tenure = record["tenure"]
            charges = record["monthly_charges"]
            is_m2m = 1 if record["contract"] == "Month-to-month" else 0
            is_elec = 1 if record.get("payment_method") == "Electronic check" else 0

            logit = -1.2 + (charges / 55.0) - (tenure / 22.0) + (1.0 * is_m2m) + (0.3 * is_elec)
            prob = float(np.round(1.0 / (1.0 + np.exp(-logit)), 4))
            pred = 1 if prob >= 0.5 else 0

            cust_id = record["customer_id"]
            feat_hash = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]

            results.append(SinglePredictionResult(
                customer_id=cust_id,
                churn_probability=prob,
                churn_prediction=pred,
            ))

            log_entries.append({
                "request_id": f"req-{cust_id}",
                "trace_id": trace_id,
                "model_version": MODEL_VERSION,
                "customer_id": cust_id,
                "features_hash": feat_hash,
                "churn_probability": prob,
                "prediction": pred,
            })
    else:
        # Scikit-learn Pipeline inference
        numeric_features = ["tenure", "monthly_charges", "total_charges"]
        categorical_features = [
            "gender", "partner", "dependents", "phone_service", "multiple_lines",
            "internet_service", "online_security", "online_backup", "device_protection",
            "tech_support", "streaming_tv", "streaming_movies", "contract",
            "paperless_billing", "payment_method"
        ]
        for col in numeric_features + categorical_features:
            if col not in df_instances.columns:
                df_instances[col] = 0.0 if col in numeric_features else "Missing"

        probs = MODEL_PIPELINE.predict_proba(df_instances[numeric_features + categorical_features])[:, 1]
        for idx, record in enumerate(records):
            prob = float(np.round(probs[idx], 4))
            pred = 1 if prob >= 0.5 else 0
            cust_id = record["customer_id"]
            feat_hash = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]

            results.append(SinglePredictionResult(
                customer_id=cust_id,
                churn_probability=prob,
                churn_prediction=pred,
            ))
            log_entries.append({
                "request_id": f"req-{cust_id}",
                "trace_id": trace_id,
                "model_version": MODEL_VERSION,
                "customer_id": cust_id,
                "features_hash": feat_hash,
                "churn_probability": prob,
                "prediction": pred,
            })

    latency_ms = float(np.round((time.perf_counter() - start_time) * 1000.0, 2))

    # Async log to file / PubSub / BigQuery log table
    log_batch_file = os.path.join(LOG_DIR, f"log_{trace_id}_{int(time.time()*1000)}.jsonl")
    with open(log_batch_file, "a", encoding="utf-8") as f:
        for entry in log_entries:
            entry["latency_ms"] = latency_ms
            entry["logged_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            f.write(json.dumps(entry) + "\n")

    emit_log(
        "INFO",
        f"Served {len(results)} predictions in {latency_ms} ms (p95 budget < 300 ms)",
        trace_id=trace_id,
        latency_ms=latency_ms,
        instance_count=len(results),
    )

    return PredictResponse(
        model_version=MODEL_VERSION,
        predictions=results,
        latency_ms=latency_ms,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
