"""
Drift statistics (Person 1, Phase D).

Numeric features: PSI on reference quantile buckets plus a two-sample KS test.
Categorical features: Jensen-Shannon distance plus a chi-square test of the
category mix, and an explicit check for categories the champion never saw.

Evidently is used for the shareable HTML report when it is installed; these
functions stay dependency-light so unit tests and Cloud Run cold starts are fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.spatial.distance import jensenshannon
from scipy.stats import chi2_contingency, ks_2samp

SMOOTHING = 1e-6


@dataclass
class FeatureDrift:
    feature: str
    type: str
    metric: str
    score: float
    threshold: float
    severity: str
    is_drifted: bool
    detail: dict = field(default_factory=dict)

    def as_event_item(self) -> dict:
        """Shape required by contracts/events/drift.detected.json."""
        return {
            "feature": self.feature,
            "metric": self.metric,
            "score": self.score,
            "threshold": self.threshold,
        }

    def as_dict(self) -> dict:
        return {
            "feature": self.feature,
            "type": self.type,
            "metric": self.metric,
            "score": self.score,
            "threshold": self.threshold,
            "severity": self.severity,
            "is_drifted": self.is_drifted,
            **self.detail,
        }


def calculate_psi(expected: np.ndarray | pd.Series, actual: np.ndarray | pd.Series, num_buckets: int = 10) -> float:
    """Population Stability Index over quantile buckets of the reference sample."""
    expected = pd.to_numeric(pd.Series(expected), errors="coerce").dropna().to_numpy()
    actual = pd.to_numeric(pd.Series(actual), errors="coerce").dropna().to_numpy()
    if expected.size == 0 or actual.size == 0:
        return 0.0

    edges = np.unique(np.percentile(expected, np.linspace(0, 100, num_buckets + 1)))
    if edges.size < 2:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf

    expected_counts, _ = np.histogram(expected, bins=edges)
    actual_counts, _ = np.histogram(actual, bins=edges)

    expected_pct = (expected_counts + SMOOTHING) / (expected_counts.sum() + SMOOTHING * len(expected_counts))
    actual_pct = (actual_counts + SMOOTHING) / (actual_counts.sum() + SMOOTHING * len(actual_counts))

    return float(np.round(np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct)), 4))


def calculate_js_distance(expected: pd.Series, actual: pd.Series) -> tuple[float, list[str]]:
    """Jensen-Shannon distance between category mixes, plus any unseen categories."""
    expected_counts = expected.astype(str).value_counts()
    actual_counts = actual.astype(str).value_counts()
    categories = sorted(set(expected_counts.index) | set(actual_counts.index))

    expected_prob = np.array([expected_counts.get(c, 0) + SMOOTHING for c in categories], dtype=float)
    actual_prob = np.array([actual_counts.get(c, 0) + SMOOTHING for c in categories], dtype=float)
    expected_prob /= expected_prob.sum()
    actual_prob /= actual_prob.sum()

    distance = float(np.round(jensenshannon(expected_prob, actual_prob) or 0.0, 4))
    unseen = sorted(set(actual_counts.index) - set(expected_counts.index))
    return distance, unseen


def chi_square_p_value(expected: pd.Series, actual: pd.Series) -> float:
    """Chi-square test that the two category mixes came from the same distribution."""
    expected_counts = expected.astype(str).value_counts()
    actual_counts = actual.astype(str).value_counts()
    categories = sorted(set(expected_counts.index) | set(actual_counts.index))
    if len(categories) < 2:
        return 1.0

    table = np.array(
        [
            [expected_counts.get(c, 0) + 1 for c in categories],
            [actual_counts.get(c, 0) + 1 for c in categories],
        ],
        dtype=float,
    )
    try:
        _, p_value, _, _ = chi2_contingency(table)
    except ValueError:  # pragma: no cover - degenerate table
        return 1.0
    return float(np.round(p_value, 6))


def ks_test(expected: pd.Series | np.ndarray, actual: pd.Series | np.ndarray) -> tuple[float, float]:
    expected = pd.to_numeric(pd.Series(expected), errors="coerce").dropna().to_numpy()
    actual = pd.to_numeric(pd.Series(actual), errors="coerce").dropna().to_numpy()
    if expected.size == 0 or actual.size == 0:
        return 0.0, 1.0
    statistic, p_value = ks_2samp(expected, actual)
    return float(np.round(statistic, 4)), float(np.round(p_value, 6))


def score_numeric_feature(
    name: str,
    reference: pd.Series,
    current: pd.Series,
    warn_threshold: float,
    critical_threshold: float,
    buckets: int = 10,
) -> FeatureDrift:
    psi = calculate_psi(reference, current, buckets)
    statistic, p_value = ks_test(reference, current)
    severity = "critical" if psi >= critical_threshold else ("warn" if psi >= warn_threshold else "none")

    return FeatureDrift(
        feature=name,
        type="numeric",
        metric="psi",
        score=psi,
        threshold=critical_threshold,
        severity=severity,
        is_drifted=severity != "none",
        detail={
            "ks_statistic": statistic,
            "ks_p_value": p_value,
            "reference_mean": round(float(pd.to_numeric(reference, errors="coerce").mean()), 4),
            "current_mean": round(float(pd.to_numeric(current, errors="coerce").mean()), 4),
        },
    )


def score_categorical_feature(
    name: str,
    reference: pd.Series,
    current: pd.Series,
    warn_threshold: float,
    critical_threshold: float,
) -> FeatureDrift:
    distance, unseen = calculate_js_distance(reference, current)
    p_value = chi_square_p_value(reference, current)

    severity = "critical" if distance >= critical_threshold else ("warn" if distance >= warn_threshold else "none")
    if unseen:  # a category the champion has never seen is always critical
        severity = "critical"

    return FeatureDrift(
        feature=name,
        type="categorical",
        metric="jensen_shannon",
        score=distance,
        threshold=critical_threshold,
        severity=severity,
        is_drifted=severity != "none",
        detail={
            "chi_square_p_value": p_value,
            "unseen_categories": unseen,
            "reference_top": str(reference.astype(str).mode().iloc[0]) if len(reference) else None,
            "current_top": str(current.astype(str).mode().iloc[0]) if len(current) else None,
        },
    )


def percent_change(reference_mean: float, current_mean: float) -> float:
    if not reference_mean:
        return 0.0
    return float(np.round((current_mean - reference_mean) / abs(reference_mean) * 100.0, 2))
