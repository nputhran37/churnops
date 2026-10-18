"""
The raw_events -> features_daily transform, derived from contracts/features.yaml.

This module is the only implementation of the transform. The ingest job calls
`FeatureTransformer.transform()`; the BigQuery path uses the SQL emitted by
`FeatureTransformer.to_sql()`, generated from the same specs and the same fitted
constants, so Python and SQL cannot drift apart (tests/unit/test_feature_parity.py).

Encoding conventions (agreed with P2):
  identity        -> column passed through unchanged
  binary_encode   -> Yes/No becomes 1/0 in <name>_encoded
  min_max_scale   -> (x - min) / (max - min) in <name>_scaled, using contract bounds
  standard_scale  -> (x - mean) / std in <name>_scaled, using fitted seed statistics
  log1p           -> ln(1 + x) in <name>_log1p
  one_hot         -> left as the raw category string; the model pipeline owns the
                     one-hot expansion so an unseen category never breaks the warehouse.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .contracts import FeatureContract, FeatureSpec, contracts_dir, load_feature_contract

ROUND_DECIMALS = 4
STATS_FILENAME = "feature_stats.json"

# Fallback statistics so the transform is usable before the seed has been profiled.
DEFAULT_STATS: dict[str, dict[str, float]] = {
    "monthly_charges": {"mean": 64.76, "std": 30.09},
    "total_charges": {"mean": 2283.3, "std": 2266.77},
    "tenure": {"mean": 32.37, "std": 24.56},
}


def stats_path() -> str:
    return os.path.join(contracts_dir(), STATS_FILENAME)


def load_feature_stats(path: str | None = None) -> dict[str, dict[str, float]]:
    """Fitted per-column statistics used by the scaling transforms."""
    path = path or stats_path()
    if not os.path.exists(path):
        return dict(DEFAULT_STATS)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    stats = dict(DEFAULT_STATS)
    stats.update(payload.get("numeric", {}))
    stats.update(payload.get("categorical", {}))
    return stats


def save_feature_stats(
    numeric: dict[str, Any],
    source: str,
    categorical: dict[str, Any] | None = None,
    path: str | None = None,
) -> str:
    """Persist fitted statistics as a reviewable contract artifact."""
    path = path or stats_path()
    payload = {
        "version": "1.0.0",
        "fitted_from": source,
        "numeric": numeric,
        "categorical": categorical or {},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


@dataclass
class FeatureTransformer:
    contract: FeatureContract
    stats: dict[str, dict[str, float]]

    @classmethod
    def from_contracts(cls, contract: FeatureContract | None = None) -> FeatureTransformer:
        return cls(contract=contract or load_feature_contract(), stats=load_feature_stats())

    # ------------------------------------------------------------------
    # imputation
    # ------------------------------------------------------------------
    def _impute(self, series: pd.Series, spec: FeatureSpec) -> pd.Series:
        rule = spec.impute
        if rule is None or not series.isna().any():
            return series
        if rule == "median":
            return series.fillna(series.median())
        if rule == "mean":
            return series.fillna(series.mean())
        if rule == "mode":
            mode = series.mode()
            return series.fillna(mode.iloc[0] if len(mode) else "")
        return series.fillna(rule)

    # ------------------------------------------------------------------
    # python transform
    # ------------------------------------------------------------------
    def _apply(self, series: pd.Series, spec: FeatureSpec) -> pd.Series:
        series = self._impute(series, spec)

        if spec.transform in ("identity", "one_hot"):
            return series

        if spec.transform == "binary_encode":
            return (series.astype(str) == "Yes").astype("int64")

        if spec.transform == "min_max_scale":
            low = float(spec.minimum if spec.minimum is not None else series.min())
            high = float(spec.maximum if spec.maximum is not None else series.max())
            span = (high - low) or 1.0
            return ((series.astype(float) - low) / span).round(ROUND_DECIMALS)

        if spec.transform == "standard_scale":
            stat = self.stats.get(spec.name, {})
            mean = float(stat.get("mean", series.mean()))
            std = float(stat.get("std", series.std()) or 1.0)
            return ((series.astype(float) - mean) / std).round(ROUND_DECIMALS)

        if spec.transform == "log1p":
            return np.log1p(series.astype(float).fillna(0.0)).round(ROUND_DECIMALS)

        raise ValueError(f"Unknown transform '{spec.transform}' for feature '{spec.name}'")

    def feature_daily_columns(self) -> list[str]:
        columns = ["customer_id"] + [spec.output_column for spec in self.contract.features]
        columns += [self.contract.target_name, "customer_segment", "batch_date", "batch_id", "trace_id"]
        return columns

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """raw_events rows -> features_daily rows (column order from the BQ contract)."""
        missing = [name for name in self.contract.feature_names if name not in df.columns]
        if missing:
            raise KeyError(f"Batch is missing contract features: {missing}")

        out = pd.DataFrame(index=df.index)
        out["customer_id"] = df["customer_id"].astype(str)

        for spec in self.contract.features:
            out[spec.output_column] = self._apply(df[spec.name], spec)

        target = self.contract.target_name
        out[target] = df[target] if target in df.columns else pd.Series([pd.NA] * len(df), index=df.index)

        for meta in ("customer_segment", "batch_date", "batch_id", "trace_id"):
            if meta in df.columns:
                out[meta] = df[meta]

        return out.reset_index(drop=True)

    # ------------------------------------------------------------------
    # BigQuery transform (same specs, same constants)
    # ------------------------------------------------------------------
    def _impute_literal(self, spec: FeatureSpec) -> str | None:
        """SQL literal for this feature's impute rule, resolving mode/median/mean from fitted stats."""
        rule = spec.impute
        if rule is None:
            return None
        if isinstance(rule, (int, float)) and not isinstance(rule, bool):
            return str(rule)
        if rule in ("mode", "median", "mean"):
            fitted = self.stats.get(spec.name, {}).get(rule)
            if fitted is None:
                return None
            return f"'{fitted}'" if isinstance(fitted, str) else str(fitted)
        return f"'{rule}'"

    def _sql_expression(self, spec: FeatureSpec) -> str:
        column = "`" + spec.name + "`"
        impute = spec.impute

        if spec.transform in ("identity", "one_hot"):
            literal = self._impute_literal(spec)
            if literal is None:
                return column
            return f"IFNULL({column}, {literal})"

        if spec.transform == "binary_encode":
            default = f"'{impute}'" if isinstance(impute, str) else "'No'"
            return f"CASE WHEN IFNULL({column}, {default}) = 'Yes' THEN 1 ELSE 0 END"

        if spec.transform == "min_max_scale":
            low = float(spec.minimum or 0.0)
            high = float(spec.maximum or 1.0)
            span = (high - low) or 1.0
            fill = impute if isinstance(impute, (int, float)) else 0
            return f"ROUND((CAST(IFNULL({column}, {fill}) AS FLOAT64) - {low}) / {span}, {ROUND_DECIMALS})"

        if spec.transform == "standard_scale":
            stat = self.stats.get(spec.name, {})
            mean = float(stat.get("mean", 0.0))
            std = float(stat.get("std", 1.0)) or 1.0
            fill = self._impute_literal(spec) or str(mean)
            base = f"IFNULL({column}, {fill})"
            return f"ROUND((CAST({base} AS FLOAT64) - {mean}) / {std}, {ROUND_DECIMALS})"

        if spec.transform == "log1p":
            return f"ROUND(LN(1 + IFNULL(CAST({column} AS FLOAT64), 0.0)), {ROUND_DECIMALS})"

        raise ValueError(f"Unknown transform '{spec.transform}' for feature '{spec.name}'")

    def to_sql(self, source_table: str, dest_table: str) -> str:
        """Emit the idempotent BigQuery MERGE that materialises features_daily."""
        projections = ["    CAST(customer_id AS STRING) AS customer_id"]
        for spec in self.contract.features:
            projections.append(f"    {self._sql_expression(spec)} AS {spec.output_column}")
        projections.append(f"    {self.contract.target_name}")
        projections += ["    customer_segment", "    batch_date", "    batch_id", "    trace_id"]

        select_block = ",\n".join(projections)
        columns = self.feature_daily_columns()
        column_list = ", ".join(columns)
        source_list = ", ".join(f"source.{c}" for c in columns)
        update_block = ",\n".join(f"  target.{c} = source.{c}" for c in columns if c != "customer_id")

        return (
            f"-- Generated from contracts/features.yaml v{self.contract.version} by data/common/features.py\n"
            "-- Idempotent: re-running a batch replaces its rows instead of duplicating them.\n"
            f"MERGE `{dest_table}` AS target\n"
            "USING (\n"
            f"  SELECT\n{select_block}\n"
            f"  FROM `{source_table}`\n"
            "  WHERE batch_id = @batch_id\n"
            ") AS source\n"
            "ON target.batch_id = source.batch_id AND target.customer_id = source.customer_id\n"
            f"WHEN MATCHED THEN UPDATE SET\n{update_block}\n"
            f"WHEN NOT MATCHED THEN INSERT ({column_list})\n"
            f"VALUES ({source_list});\n"
        )


def transform_features_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Convenience wrapper used by the ingest job and tests."""
    return FeatureTransformer.from_contracts().transform(df)
