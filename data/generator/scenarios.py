"""
Generic drift-injection engine (Person 1, Phase B).

Scenarios are data, not code: `scenarios.yaml` lists operations, this module knows
how to apply each operation type, and severity (0-1) scales every magnitude and
fraction linearly. A new business story is a YAML entry plus a golden test.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import yaml
from common.contracts import FeatureContract, load_feature_contract

SCENARIOS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios.yaml")


class ScenarioError(ValueError):
    """Raised for an unknown scenario or an unsupported operation type."""


@dataclass
class Scenario:
    name: str
    description: str
    business_narrative: str
    expected_drift: list[str]
    expect_quarantine: bool
    operations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScenarioCatalogue:
    scenarios: dict[str, Scenario]
    version: str = "2.0.0"

    @classmethod
    def load(cls, path: str = SCENARIOS_PATH) -> ScenarioCatalogue:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        scenarios = {
            name: Scenario(
                name=name,
                description=spec.get("description", ""),
                business_narrative=" ".join(spec.get("business_narrative", "").split()),
                expected_drift=spec.get("expected_drift", []),
                expect_quarantine=bool(spec.get("expect_quarantine", False)),
                operations=spec.get("operations") or [],
            )
            for name, spec in (raw.get("scenarios") or {}).items()
        }
        return cls(scenarios=scenarios, version=raw.get("version", "2.0.0"))

    @property
    def names(self) -> list[str]:
        return sorted(self.scenarios)

    def get(self, name: str) -> Scenario:
        if name not in self.scenarios:
            raise ScenarioError(f"Unknown scenario '{name}'. Available: {', '.join(self.names)}")
        return self.scenarios[name]


# ----------------------------------------------------------------------
# operation primitives
# ----------------------------------------------------------------------
def _mask(df: pd.DataFrame, operation: dict, severity: float, rng: np.random.Generator) -> np.ndarray:
    """Rows this operation applies to: segment filter first, then a random fraction."""
    selected = np.ones(len(df), dtype=bool)

    segments = operation.get("segments")
    if segments and "customer_segment" in df.columns:
        selected &= df["customer_segment"].astype(str).isin(segments).to_numpy()

    fraction = operation.get("fraction")
    if fraction is not None:
        effective = float(fraction) * severity
        selected &= rng.random(len(df)) < effective

    return selected


def _clip(series: pd.Series, operation: dict, spec_min: float | None, spec_max: float | None) -> pd.Series:
    if not operation.get("clip_to_contract", True):
        return series
    low = operation.get("min_value", spec_min)
    high = operation.get("max_value", spec_max)
    if low is not None:
        series = series.clip(lower=float(low))
    if high is not None:
        series = series.clip(upper=float(high))
    return series


def _assign_numeric(df: pd.DataFrame, column: str, mask, values: pd.Series, operation: dict, contract) -> pd.DataFrame:
    """Write numeric values back, preserving the column's contract dtype."""
    clipped = _clip(values, operation, *_bounds(contract, column))
    if pd.api.types.is_integer_dtype(df[column].dtype):
        df[column] = df[column].astype("float64")
        df.loc[mask, column] = clipped.round(0)
        if not clipped.isna().any() and not df[column].isna().any():
            df[column] = df[column].round(0).astype("int64")
    else:
        df.loc[mask, column] = clipped.round(2)
    return df


def _apply_multiplicative(df, column, mask, operation, severity, contract, rng):
    factor = 1.0 + float(operation["magnitude"]) * severity
    values = pd.to_numeric(df.loc[mask, column], errors="coerce") * factor
    return _assign_numeric(df, column, mask, values, operation, contract)


def _apply_additive(df, column, mask, operation, severity, contract, rng):
    delta = float(operation["magnitude"]) * severity
    values = pd.to_numeric(df.loc[mask, column], errors="coerce") + delta
    return _assign_numeric(df, column, mask, values, operation, contract)


def _apply_noise_scale(df, column, mask, operation, severity, contract, rng):
    series = pd.to_numeric(df.loc[mask, column], errors="coerce")
    centre = series.mean()
    widened = centre + (series - centre) * (1.0 + float(operation["magnitude"]) * severity)
    return _assign_numeric(df, column, mask, widened, operation, contract)


def _apply_category_shift(df, column, mask, operation, severity, contract, rng):
    df.loc[mask, column] = operation["value"]
    return df


def _apply_new_category(df, column, mask, operation, severity, contract, rng):
    df.loc[mask, column] = operation["value"]
    return df


def _apply_null_injection(df, column, mask, operation, severity, contract, rng):
    df.loc[mask, column] = np.nan
    return df


def _apply_range_violation(df, column, mask, operation, severity, contract, rng):
    df.loc[mask, column] = operation.get("value", -1)
    return df


def _apply_label_odds_shift(df, column, mask, operation, severity, contract, rng):
    """Push the churn label rate the way the business story implies."""
    multiplier = max(1.0 + float(operation["magnitude"]) * severity, 0.01)
    labels = pd.to_numeric(df.loc[mask, "churn"], errors="coerce").fillna(0).astype(int)
    base_rate = float(labels.mean()) if len(labels) else 0.0
    odds = base_rate / max(1e-6, 1.0 - base_rate)
    target_rate = min(0.95, (odds * multiplier) / (1.0 + odds * multiplier))

    if target_rate > base_rate:
        flip_pool = labels.index[labels == 0]
        needed = int(round((target_rate - base_rate) * len(labels)))
    else:
        flip_pool = labels.index[labels == 1]
        needed = int(round((base_rate - target_rate) * len(labels)))

    needed = min(needed, len(flip_pool))
    if needed > 0:
        chosen = rng.choice(np.asarray(flip_pool), size=needed, replace=False)
        df.loc[chosen, "churn"] = 1 if target_rate > base_rate else 0
    return df


OPERATIONS: dict[str, Callable] = {
    "multiplicative_shift": _apply_multiplicative,
    "additive_shift": _apply_additive,
    "noise_scale": _apply_noise_scale,
    "category_shift": _apply_category_shift,
    "new_category": _apply_new_category,
    "null_injection": _apply_null_injection,
    "range_violation": _apply_range_violation,
    "label_odds_shift": _apply_label_odds_shift,
}


def _bounds(contract: FeatureContract, column: str) -> tuple[float | None, float | None]:
    try:
        spec = contract.get(column)
    except KeyError:
        return None, None
    return spec.minimum, spec.maximum


def apply_scenario(
    df: pd.DataFrame,
    scenario: Scenario,
    severity: float,
    contract: FeatureContract | None = None,
    rng: np.random.Generator | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Apply every operation of a scenario; returns the drifted frame and an audit trail."""
    contract = contract or load_feature_contract()
    rng = rng or np.random.default_rng()
    severity = float(min(max(severity, 0.0), 1.0))

    out = df.copy()
    audit: list[dict] = []
    if severity == 0.0 or not scenario.operations:
        return out, audit

    for operation in scenario.operations:
        op_type = operation["type"]
        column = operation["feature"]
        handler = OPERATIONS.get(op_type)
        if handler is None:
            raise ScenarioError(f"Unsupported operation type '{op_type}' in scenario '{scenario.name}'")
        if column not in out.columns:
            raise ScenarioError(f"Scenario '{scenario.name}' targets unknown column '{column}'")

        mask = _mask(out, operation, severity, rng)
        before = _summarise(out[column])
        out = handler(out, column, mask, operation, severity, contract, rng)
        audit.append(
            {
                "feature": column,
                "operation": op_type,
                "rows_affected": int(mask.sum()),
                "severity": severity,
                "before": before,
                "after": _summarise(out[column]),
            }
        )

    return out, audit


def _summarise(series: pd.Series) -> dict:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().any():
        return {
            "mean": round(float(numeric.mean()), 4),
            "null_fraction": round(float(series.isna().mean()), 4),
        }
    counts = series.astype(str).value_counts(normalize=True)
    return {
        "top_category": str(counts.index[0]) if len(counts) else None,
        "distinct": int(series.nunique(dropna=True)),
        "null_fraction": round(float(series.isna().mean()), 4),
    }
