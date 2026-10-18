"""
Distribution profile for the synthetic generator (Person 1, Phase B).

Rather than hard-coding sampling constants, the generator fits a profile from the
seed dataset once (`make seed`) and samples from it afterwards. The profile keeps
per-column marginals plus the few correlations that make the data believable
(charges given internet service, tenure given contract) and a small logistic churn
model, so a drifted batch moves the label rate the way the business story says.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from common.contracts import FeatureContract, load_feature_contract

PROFILE_FILENAME = "profile.json"
PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), PROFILE_FILENAME)

# Columns whose value is dictated by another column, not sampled independently.
DEPENDENT_SERVICES = [
    "online_security",
    "online_backup",
    "device_protection",
    "tech_support",
    "streaming_tv",
    "streaming_movies",
]

# Features fed into the fitted logistic churn model.
CHURN_MODEL_FEATURES = ["tenure", "monthly_charges", "is_month_to_month", "is_electronic_check", "senior_citizen"]


@dataclass
class DistributionProfile:
    """Marginals, conditionals and a churn model fitted from the seed dataset."""

    numeric: dict[str, dict[str, float]] = field(default_factory=dict)
    categorical: dict[str, dict[str, float]] = field(default_factory=dict)
    conditional: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    churn_model: dict[str, Any] = field(default_factory=dict)
    segment_bounds: dict[str, float] = field(default_factory=lambda: {"premium": 50.0, "enterprise": 85.0})
    row_count: int = 0
    fitted_from: str = "unknown"
    version: str = "1.0.0"

    # ------------------------------------------------------------------
    # fit / persist
    # ------------------------------------------------------------------
    @classmethod
    def fit(cls, df: pd.DataFrame, source: str, contract: FeatureContract | None = None) -> DistributionProfile:
        contract = contract or load_feature_contract()
        profile = cls(fitted_from=source, row_count=len(df))

        for name in contract.numeric_features:
            series = pd.to_numeric(df[name], errors="coerce").dropna()
            profile.numeric[name] = {
                "mean": float(series.mean()),
                "std": float(series.std() or 1.0),
                "min": float(series.min()),
                "max": float(series.max()),
                "median": float(series.median()),
                "q25": float(series.quantile(0.25)),
                "q75": float(series.quantile(0.75)),
            }

        for name in contract.categorical_features:
            counts = df[name].astype(str).value_counts(normalize=True)
            allowed = contract.allowed_values(name) or list(counts.index)
            probabilities = {str(value): float(counts.get(str(value), 0.0)) for value in allowed}
            total = sum(probabilities.values()) or 1.0
            profile.categorical[name] = {k: v / total for k, v in probabilities.items()}
            profile.categorical[name]["__mode__"] = str(counts.idxmax()) if len(counts) else ""

        profile.conditional["monthly_charges|internet_service"] = _conditional_stats(
            df, "monthly_charges", "internet_service"
        )
        profile.conditional["tenure|contract"] = _conditional_stats(df, "tenure", "contract")
        profile.churn_model = _fit_churn_model(df)
        return profile

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "fitted_from": self.fitted_from,
            "row_count": self.row_count,
            "segment_bounds": self.segment_bounds,
            "numeric": self.numeric,
            "categorical": self.categorical,
            "conditional": self.conditional,
            "churn_model": self.churn_model,
        }

    def save(self, path: str = PROFILE_PATH) -> str:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
        return path

    @classmethod
    def load(cls, path: str = PROFILE_PATH) -> DistributionProfile:
        """Load the fitted profile, falling back to the built-in Telco-like priors."""
        if not os.path.exists(path):
            return cls.builtin()
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls(
            numeric=payload.get("numeric", {}),
            categorical=payload.get("categorical", {}),
            conditional=payload.get("conditional", {}),
            churn_model=payload.get("churn_model", {}),
            segment_bounds=payload.get("segment_bounds", {"premium": 50.0, "enterprise": 85.0}),
            row_count=payload.get("row_count", 0),
            fitted_from=payload.get("fitted_from", "unknown"),
            version=payload.get("version", "1.0.0"),
        )

    # ------------------------------------------------------------------
    # priors used before a seed exists (keeps the generator runnable on a fresh clone)
    # ------------------------------------------------------------------
    @classmethod
    def builtin(cls) -> DistributionProfile:
        return cls(
            fitted_from="builtin-telco-priors",
            numeric={
                "tenure": {"mean": 32.4, "std": 24.6, "min": 1, "max": 72, "median": 29, "q25": 9, "q75": 55},
                "monthly_charges": {
                    "mean": 64.8, "std": 30.1, "min": 18.25, "max": 118.75, "median": 70.35,
                    "q25": 35.5, "q75": 89.85,
                },
                "total_charges": {
                    "mean": 2283.3, "std": 2266.8, "min": 18.8, "max": 8684.8, "median": 1397.5,
                    "q25": 401.4, "q75": 3794.7,
                },
            },
            categorical={
                "gender": {"Male": 0.50, "Female": 0.50, "__mode__": "Male"},
                "senior_citizen": {"0": 0.84, "1": 0.16, "__mode__": "0"},
                "partner": {"Yes": 0.48, "No": 0.52, "__mode__": "No"},
                "dependents": {"Yes": 0.30, "No": 0.70, "__mode__": "No"},
                "phone_service": {"Yes": 0.90, "No": 0.10, "__mode__": "Yes"},
                "multiple_lines": {"Yes": 0.42, "No": 0.48, "No phone service": 0.10, "__mode__": "No"},
                "internet_service": {"DSL": 0.34, "Fiber optic": 0.44, "No": 0.22, "__mode__": "Fiber optic"},
                "online_security": {"Yes": 0.29, "No": 0.49, "No internet service": 0.22, "__mode__": "No"},
                "online_backup": {"Yes": 0.34, "No": 0.44, "No internet service": 0.22, "__mode__": "No"},
                "device_protection": {"Yes": 0.34, "No": 0.44, "No internet service": 0.22, "__mode__": "No"},
                "tech_support": {"Yes": 0.29, "No": 0.49, "No internet service": 0.22, "__mode__": "No"},
                "streaming_tv": {"Yes": 0.38, "No": 0.40, "No internet service": 0.22, "__mode__": "No"},
                "streaming_movies": {"Yes": 0.39, "No": 0.39, "No internet service": 0.22, "__mode__": "No"},
                "contract": {"Month-to-month": 0.55, "One year": 0.21, "Two year": 0.24, "__mode__": "Month-to-month"},
                "paperless_billing": {"Yes": 0.59, "No": 0.41, "__mode__": "Yes"},
                "payment_method": {
                    "Electronic check": 0.33,
                    "Mailed check": 0.23,
                    "Bank transfer (automatic)": 0.22,
                    "Credit card (automatic)": 0.22,
                    "__mode__": "Electronic check",
                },
            },
            conditional={
                "monthly_charges|internet_service": {
                    "Fiber optic": {"mean": 85.0, "std": 10.0},
                    "DSL": {"mean": 55.0, "std": 8.0},
                    "No": {"mean": 22.0, "std": 4.0},
                },
                "tenure|contract": {
                    "Month-to-month": {"mean": 18.0, "std": 17.0},
                    "One year": {"mean": 42.0, "std": 19.0},
                    "Two year": {"mean": 57.0, "std": 17.0},
                },
            },
            churn_model={
                "intercept": -2.0,
                "coefficients": {
                    "tenure": -0.040,
                    "monthly_charges": 0.020,
                    "is_month_to_month": 1.20,
                    "is_electronic_check": 0.40,
                    "senior_citizen": 0.25,
                },
                "base_rate": 0.265,
            },
            row_count=0,
        )

    # ------------------------------------------------------------------
    # sampling helpers
    # ------------------------------------------------------------------
    def categories(self, name: str) -> tuple[list[str], list[float]]:
        dist = {k: v for k, v in self.categorical.get(name, {}).items() if k != "__mode__"}
        if not dist:
            return ["Unknown"], [1.0]
        total = sum(dist.values()) or 1.0
        return list(dist.keys()), [v / total for v in dist.values()]

    def conditional_stats(self, key: str, level: str, fallback: str) -> dict[str, float]:
        table = self.conditional.get(key, {})
        if level in table:
            return table[level]
        return self.numeric.get(fallback, {"mean": 0.0, "std": 1.0})

    def churn_probability(self, frame: pd.DataFrame) -> np.ndarray:
        model = self.churn_model or self.builtin().churn_model
        logit = np.full(len(frame), float(model.get("intercept", 0.0)))
        for feature, weight in model.get("coefficients", {}).items():
            logit = logit + float(weight) * _model_feature(frame, feature).to_numpy(dtype=float)
        return 1.0 / (1.0 + np.exp(-logit))


def _model_feature(df: pd.DataFrame, feature: str) -> pd.Series:
    if feature == "is_month_to_month":
        return (df["contract"].astype(str) == "Month-to-month").astype(float)
    if feature == "is_electronic_check":
        return (df["payment_method"].astype(str) == "Electronic check").astype(float)
    if feature not in df.columns:
        return pd.Series(np.zeros(len(df)), index=df.index)
    return pd.to_numeric(df[feature], errors="coerce").fillna(0.0)


def _conditional_stats(df: pd.DataFrame, numeric_col: str, by_col: str) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for level, group in df.groupby(df[by_col].astype(str)):
        series = pd.to_numeric(group[numeric_col], errors="coerce").dropna()
        if series.empty:
            continue
        stats[str(level)] = {"mean": float(series.mean()), "std": float(series.std() or 1.0)}
    return stats


def _fit_churn_model(df: pd.DataFrame) -> dict[str, Any]:
    """Fit the small logistic model that drives label generation."""
    if "churn" not in df.columns or df["churn"].nunique() < 2:
        return DistributionProfile.builtin().churn_model

    frame = pd.DataFrame(
        {name: _model_feature(df, name) for name in CHURN_MODEL_FEATURES},
        index=df.index,
    )
    target = pd.to_numeric(df["churn"], errors="coerce").fillna(0).astype(int)

    try:
        from sklearn.linear_model import LogisticRegression

        model = LogisticRegression(max_iter=500)
        model.fit(frame.to_numpy(dtype=float), target.to_numpy())
        coefficients = {name: float(w) for name, w in zip(CHURN_MODEL_FEATURES, model.coef_[0], strict=True)}
        intercept = float(model.intercept_[0])
    except Exception:  # pragma: no cover - sklearn is optional in the generator image
        builtin = DistributionProfile.builtin().churn_model
        coefficients, intercept = builtin["coefficients"], builtin["intercept"]

    return {
        "intercept": intercept,
        "coefficients": coefficients,
        "base_rate": float(target.mean()),
    }


def truncated_normal(
    rng: np.random.Generator,
    mean: float,
    std: float,
    low: float,
    high: float,
    size: int,
) -> np.ndarray:
    """Normal samples clipped into the contract's allowed range."""
    std = max(float(std), 1e-6)
    draws = rng.normal(mean, std, size=size)
    return np.clip(draws, low, high)


def logistic(x: np.ndarray | float) -> np.ndarray | float:
    if isinstance(x, (int, float)):
        return 1.0 / (1.0 + math.exp(-x))
    return 1.0 / (1.0 + np.exp(-x))
