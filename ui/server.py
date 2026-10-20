"""
ChurnOps Interactive Dashboard Server
FastAPI backend powering the visual MLOps demo dashboard.
"""

from __future__ import annotations

import contextlib
import datetime
import glob
import io
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for path in (
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "data"),
    os.path.join(PROJECT_ROOT, "data", "common"),
    os.path.join(PROJECT_ROOT, "data", "generator"),
    os.path.join(PROJECT_ROOT, "data", "ingest"),
    os.path.join(PROJECT_ROOT, "data", "drift"),
    os.path.join(PROJECT_ROOT, "ml", "pipeline"),
    os.path.join(PROJECT_ROOT, "llm", "analyst"),
    os.path.join(PROJECT_ROOT, "scripts"),
):
    if path not in sys.path:
        sys.path.insert(0, path)

import joblib
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from common.warehouse import Warehouse
from reference import ReferenceWindow
from scenarios import ScenarioCatalogue
import simulate_walking_skeleton as simulator

app = FastAPI(title="ChurnOps Live Demo Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Cache for active model
_CACHED_MODEL = None
_CACHED_MODEL_VERSION = None


def get_latest_champion_model():
    global _CACHED_MODEL, _CACHED_MODEL_VERSION
    model_dirs = sorted(glob.glob(os.path.join(PROJECT_ROOT, "artifacts", "models", "v-*")))
    if not model_dirs:
        return None, "none"
    latest_dir = model_dirs[-1]
    version_tag = os.path.basename(latest_dir)
    model_file = os.path.join(latest_dir, "model.joblib")
    if os.path.exists(model_file):
        if _CACHED_MODEL_VERSION != version_tag or _CACHED_MODEL is None:
            _CACHED_MODEL = joblib.load(model_file)
            _CACHED_MODEL_VERSION = version_tag
        return _CACHED_MODEL, version_tag
    return None, "none"


class SimulationRequest(BaseModel):
    scenario: str = "price_hike"
    severity: float = 0.8
    batch_size: int = 1500
    ignore_cooldown: bool = True


class CustomerPredictRequest(BaseModel):
    tenure: int = 2
    monthly_charges: float = 85.5
    total_charges: float = 171.0
    gender: str = "Female"
    partner: str = "No"
    dependents: str = "No"
    phone_service: str = "Yes"
    multiple_lines: str = "No"
    internet_service: str = "Fiber optic"
    online_security: str = "No"
    online_backup: str = "No"
    device_protection: str = "No"
    tech_support: str = "No"
    streaming_tv: str = "Yes"
    streaming_movies: str = "Yes"
    contract: str = "Month-to-month"
    paperless_billing: str = "Yes"
    payment_method: str = "Electronic check"


@app.get("/", response_class=HTMLResponse)
def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>Dashboard frontend is initializing...</h2>")


@app.get("/api/status")
def get_status():
    warehouse = Warehouse.default()
    ref = ReferenceWindow.load(warehouse)
    model, model_ver = get_latest_champion_model()

    catalogue = ScenarioCatalogue.load()
    scenarios_meta = []
    for name in catalogue.names:
        spec = catalogue.get(name)
        scenarios_meta.append({
            "id": name,
            "name": name.replace("_", " ").title(),
            "description": spec.business_narrative,
            "target_feature": getattr(spec, "target_feature", "N/A"),
        })

    # Recent reports
    report_dirs = sorted(glob.glob(os.path.join(PROJECT_ROOT, "reports", "drift", "b-*")), reverse=True)
    recent_reports = [os.path.basename(d) for d in report_dirs[:8]]

    return {
        "status": "online",
        "champion_model_version": ref.model_version if ref else model_ver,
        "reference_rows": ref.row_count if ref else 0,
        "reference_snapshot_date": ref.snapshot_date if ref else "None",
        "scenarios": scenarios_meta,
        "recent_reports": recent_reports,
    }


@app.post("/api/simulate")
def run_simulation(req: SimulationRequest):
    """
    Executes the full walking skeleton while capturing console logs and returning structured state.
    """
    log_stream = io.StringIO()
    start_time = time.perf_counter()

    with contextlib.redirect_stdout(log_stream):
        try:
            result = simulator.run_simulation(
                scenario=req.scenario,
                severity=req.severity,
                batch_size=req.batch_size,
                ignore_cooldown=req.ignore_cooldown,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc(file=log_stream)
            result = {
                "status": "error",
                "error": str(exc),
            }

    duration = time.perf_counter() - start_time
    logs = log_stream.getvalue().splitlines()

    # Extract drift report summary if available
    batch_info = result.get("batch", {})
    batch_id = batch_info.get("batch_id")
    report_json_path = os.path.join(PROJECT_ROOT, "reports", "drift", batch_id or "", "report.json")
    drift_details = {}
    if os.path.exists(report_json_path):
        with open(report_json_path, "r", encoding="utf-8") as f:
            drift_details = json.load(f)

    # Invalidate model cache so fresh promoted champion is picked up
    global _CACHED_MODEL
    _CACHED_MODEL = None

    return {
        "simulation_result": result,
        "drift_details": drift_details,
        "logs": logs,
        "duration_seconds": round(duration, 2),
        "batch_id": batch_id,
        "scenario": req.scenario,
        "severity": req.severity,
    }


@app.post("/api/predict")
def predict_churn(customer: CustomerPredictRequest):
    start = time.perf_counter()
    model, version = get_latest_champion_model()
    if model is None:
        raise HTTPException(status_code=503, detail="No trained champion model is available.")

    data = pd.DataFrame([customer.model_dump()])
    try:
        proba = float(model.predict_proba(data)[0][1])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Model inference failed: {e}")

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    risk = "HIGH" if proba >= 0.60 else ("MEDIUM" if proba >= 0.35 else "LOW")

    return {
        "churn_probability": round(proba, 4),
        "churn_percentage": f"{proba * 100:.1f}%",
        "risk_level": risk,
        "model_version": version,
        "latency_ms": latency_ms,
    }


@app.get("/api/reports/{batch_id}")
def get_report_html(batch_id: str):
    report_file = os.path.join(PROJECT_ROOT, "reports", "drift", batch_id, "report.html")
    if os.path.exists(report_file):
        return FileResponse(report_file, media_type="text/html")
    raise HTTPException(status_code=404, detail=f"Report for batch {batch_id} not found.")


def start():
    uvicorn.run("ui.server:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    start()
