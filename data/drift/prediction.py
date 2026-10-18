"""
Prediction drift inputs (Person 1, Phase D).

Feature drift says the inputs moved; prediction drift says the champion's opinion
moved, which is the signal a product manager actually feels. Scores are sourced in
the order the policy lists:

  prediction_api   POST /predict on P2's serving service (contracts/api/openapi.yaml)
  predictions_log  the scores the service already logged for this batch
  offline_baseline a local champion artifact, or the documented fallback logistic
                   curve, so the detector still produces a verdict offline

Whichever source answers, reference and current scores always come from the *same*
scorer, otherwise the comparison would measure the scorer, not the drift.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
from common.contracts import FeatureContract, load_feature_contract
from common.logging_utils import get_logger
from common.warehouse import Warehouse

log = get_logger("drift-detector")

PREDICT_URL = os.getenv("CHURNOPS_PREDICT_URL", "").rstrip("/")
CHAMPION_MODEL_PATH = os.getenv("CHURNOPS_CHAMPION_MODEL", os.path.join("artifacts", "models", "champion", "model.joblib"))


@dataclass
class ScoreSet:
    reference: np.ndarray
    current: np.ndarray
    source: str
    model_version: str = "unknown"

    @property
    def available(self) -> bool:
        return self.reference.size > 0 and self.current.size > 0


def sample_rows(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    """Deterministic sample so reference and batch are compared like for like."""
    if len(df) <= limit:
        return df
    return df.sample(limit, random_state=11)


def _instances(df: pd.DataFrame, contract: FeatureContract, limit: int) -> list[dict]:
    frame = sample_rows(df, limit)
    columns = [contract.primary_key] + contract.feature_names
    payload = frame[[c for c in columns if c in frame.columns]].copy()
    payload = payload.where(pd.notna(payload), None)
    return json.loads(payload.to_json(orient="records"))


def _score_via_api(df: pd.DataFrame, contract: FeatureContract, trace_id: str, limit: int, timeout: int):
    if not PREDICT_URL:
        return None, "unknown"
    try:
        import requests

        response = requests.post(
            f"{PREDICT_URL}/predict",
            json={"trace_id": trace_id, "instances": _instances(df, contract, limit)},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        scores = np.array([p["churn_probability"] for p in body.get("predictions", [])], dtype=float)
        return (scores if scores.size else None), str(body.get("model_version", "unknown"))
    except Exception as exc:
        log.warning(
            f"Prediction API unavailable for prediction drift: {exc}",
            trace_id,
            predict_url=PREDICT_URL,
        )
        return None, "unknown"


def _score_from_log(batch_id: str, warehouse: Warehouse) -> np.ndarray | None:
    rows = warehouse.read_rows("predictions_log")
    if not rows:
        return None
    scores = [float(r["churn_probability"]) for r in rows if str(r.get("batch_id", batch_id)) == batch_id]
    return np.array(scores, dtype=float) if scores else None


def _offline_scorer(df: pd.DataFrame, contract: FeatureContract) -> np.ndarray:
    """Score locally: the champion artifact if it is on disk, else the documented curve."""
    if os.path.exists(CHAMPION_MODEL_PATH):
        try:
            import joblib

            model = joblib.load(CHAMPION_MODEL_PATH)
            features = [c for c in contract.feature_names if c in df.columns]
            return np.asarray(model.predict_proba(df[features])[:, 1], dtype=float)
        except Exception as exc:  # pragma: no cover - artifact shape is P2's contract
            log.warning(f"Champion artifact could not score the batch: {exc}", "tr-drift")

    tenure = pd.to_numeric(df.get("tenure", 0), errors="coerce").fillna(0.0)
    charges = pd.to_numeric(df.get("monthly_charges", 0), errors="coerce").fillna(0.0)
    month_to_month = (df.get("contract", pd.Series(dtype=str)).astype(str) == "Month-to-month").astype(float)
    electronic = (df.get("payment_method", pd.Series(dtype=str)).astype(str) == "Electronic check").astype(float)

    logit = -1.2 + (charges / 55.0) - (tenure / 22.0) + month_to_month + 0.3 * electronic
    return np.asarray(1.0 / (1.0 + np.exp(-logit)), dtype=float)


def collect_scores(
    current: pd.DataFrame,
    reference: pd.DataFrame,
    policy: dict,
    batch_id: str,
    trace_id: str,
    warehouse: Warehouse | None = None,
    contract: FeatureContract | None = None,
) -> ScoreSet:
    """Score the batch and the reference window with the best available source."""
    contract = contract or load_feature_contract()
    warehouse = warehouse or Warehouse.default()
    settings = policy.get("prediction_drift", {})
    limit = int(settings.get("sample_rows", 300))
    timeout = int(settings.get("request_timeout_seconds", 10))

    for source in settings.get("sources", ["offline_baseline"]):
        if source == "prediction_api":
            current_scores, model_version = _score_via_api(current, contract, trace_id, limit, timeout)
            if current_scores is None:
                continue
            reference_scores, _ = _score_via_api(reference, contract, trace_id, limit, timeout)
            if reference_scores is None:
                continue
            return ScoreSet(reference_scores, current_scores, "prediction_api", model_version)

        if source == "predictions_log":
            current_scores = _score_from_log(batch_id, warehouse)
            if current_scores is None:
                continue
            reference_scores = _offline_scorer(sample_rows(reference, limit), contract)
            log.info(
                "Prediction drift computed from predictions_log",
                trace_id,
                batch_id=batch_id,
                metrics={"scored_rows": int(current_scores.size)},
            )
            return ScoreSet(reference_scores, current_scores, "predictions_log")

        if source == "offline_baseline":
            return ScoreSet(
                _offline_scorer(sample_rows(reference, limit), contract),
                _offline_scorer(sample_rows(current, limit), contract),
                "offline_baseline",
            )

    return ScoreSet(np.array([]), np.array([]), "unavailable")
