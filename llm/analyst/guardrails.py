"""
ChurnOps LLMOps Guardrails & Faithfulness Validator (Person 2)
Validates Pydantic schema and prevents numerical hallucination.
"""

import re
from typing import List, Literal, Tuple
from pydantic import BaseModel, Field, ValidationError


class DriftIncidentReport(BaseModel):
    headline: str = Field(..., description="One-sentence executive summary")
    what_changed: List[str] = Field(..., description="Key feature changes and statistical metrics")
    likely_causes: List[str] = Field(..., description="Plausible business hypotheses")
    model_action: str = Field(..., description="Platform automation response")
    recommended_checks: List[str] = Field(..., description="Actionable next steps")
    confidence: Literal["high", "medium", "low"] = "high"


def extract_numbers(text: str) -> List[float]:
    """Extracts integer and float numbers from a text string."""
    # Match standard floats, ints, and percentages
    matches = re.findall(r"[-+]?(?:\d*\.\d+|\d+)", text)
    numbers = []
    for m in matches:
        try:
            val = float(m)
            # Filter out dates/years (e.g. 2026) or tiny constants if needed
            numbers.append(val)
        except ValueError:
            continue
    return numbers


def verify_numeric_guardrail(
    output_report: DriftIncidentReport,
    input_context_str: str,
    tolerance: float = 0.05,
) -> Tuple[bool, List[float]]:
    """
    Checks that every number appearing in the generated report exists in the input context.
    Returns (passed, list_of_unanchored_numbers).
    """
    report_text = f"{output_report.headline} " + " ".join(output_report.what_changed) + " " + output_report.model_action
    output_nums = extract_numbers(report_text)
    context_nums = extract_numbers(input_context_str)

    unanchored = []
    for num in output_nums:
        # Ignore common innocuous small integers (e.g. 1, 2, 3 in bullet lists), standard percentages (0, 100), hours (24, 48), or years
        if num in [0, 1, 2, 3, 4, 5, 24, 48, 100, 2026]:
            continue

        # Check if num is close to any number in context
        is_anchored = any(abs(num - c_num) <= max(tolerance, abs(c_num) * tolerance) for c_num in context_nums)
        if not is_anchored:
            unanchored.append(num)

    passed = len(unanchored) == 0
    return passed, unanchored


def generate_templated_fallback(
    event_type: str,
    batch_id: str,
    drifted_features: List[dict],
    trace_id: str,
) -> DriftIncidentReport:
    """Deterministic, guaranteed-accurate fallback report if LLM fails or hallucinates."""
    feat_lines = [f"{d.get('feature', 'unknown')} ({d.get('metric', 'PSI')}={d.get('score', 0.0):.3f})" for d in drifted_features[:3]]
    feat_summary = ", ".join(feat_lines) if feat_lines else "statistical metrics"

    if event_type == "drift.detected":
        headline = f"Data drift alert on batch {batch_id}: Significant distribution shifts observed in {len(drifted_features)} features."
        what_changed = [f"Drift detected in: {feat_summary}"]
        likely_causes = [
            "Recent pricing adjustments or promotional billing changes",
            "Shifts in customer segment acquisition mix",
        ]
        model_action = "Continuous training pipeline automatically dispatched to evaluate and retrain challenger model."
        recommended_checks = [
            "Inspect upstream ingestion logs for data contract anomalies",
            "Verify billing systems and recent marketing campaign rollouts",
        ]
    elif event_type == "model.promoted":
        headline = f"Automated model promotion completed for trace {trace_id}: New challenger promoted to champion."
        what_changed = [f"Retrained on drifted data including {feat_summary}"]
        likely_causes = ["Model successfully adapted to recent behavioral patterns"]
        model_action = "Canary traffic successfully shifted 100% to new revision; registry alias 'champion' updated."
        recommended_checks = [
            "Monitor prediction score distribution and API latency SLOs in Looker Studio",
        ]
    else:
        headline = f"Model release gated or rejected for batch {batch_id}."
        what_changed = ["Challenger model failed quality gate thresholds."]
        likely_causes = ["Holdout ROC-AUC degradation or calibration variance"]
        model_action = "Serving revision rolled back 100% to active champion; incident recorded."
        recommended_checks = ["Review feature importance weights and class balance in holdout dataset"]

    return DriftIncidentReport(
        headline=headline,
        what_changed=what_changed,
        likely_causes=likely_causes,
        model_action=model_action,
        recommended_checks=recommended_checks,
        confidence="medium",
    )
