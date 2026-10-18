"""
Pandera validation generated from contracts/features.yaml (Person 1, Phase C).

Nothing here is hand-written per column: dtypes, ranges, category sets and the
primary-key pattern all come from the contract, so bumping features.yaml
automatically tightens (or loosens) what the ingest job accepts. A failure is a
quarantine decision, never a silent coercion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

try:  # pandera >= 0.20 splits the pandas backend into its own namespace
    import pandera.pandas as pa
except ImportError:  # pragma: no cover - older pandera
    import pandera as pa

from common.contracts import FeatureContract, FeatureSpec, load_feature_contract

PANDAS_DTYPES = {"int64": "int64", "float64": "float64", "string": "object", "date": "object"}


def _column_for(spec: FeatureSpec, nullable: bool = True) -> pa.Column:
    checks: list[Any] = []
    if spec.allowed_values:
        checks.append(pa.Check.isin(list(spec.allowed_values)))
    if spec.minimum is not None and spec.maximum is not None:
        checks.append(pa.Check.in_range(spec.minimum, spec.maximum))
    elif spec.minimum is not None:
        checks.append(pa.Check.ge(spec.minimum))
    elif spec.maximum is not None:
        checks.append(pa.Check.le(spec.maximum))

    dtype = {"int64": int, "float64": float}.get(spec.dtype, str)
    return pa.Column(dtype, checks=checks or None, nullable=nullable, coerce=True, required=True)


def build_raw_events_schema(contract: FeatureContract | None = None) -> pa.DataFrameSchema:
    """Schema every landed batch must satisfy before it can touch raw_events."""
    contract = contract or load_feature_contract()
    required_non_null = set(contract.required_non_null)

    columns: dict[str, Any] = {
        contract.primary_key: pa.Column(
            str,
            checks=[pa.Check.str_matches(contract.customer_id_pattern)],
            nullable=False,
            unique=True,
            coerce=True,
        )
    }

    for spec in contract.features:
        columns[spec.name] = _column_for(spec, nullable=spec.name not in required_non_null)

    # Target may be absent/NULL on landing: labels arrive two simulated weeks later.
    columns[contract.target_name] = pa.Column(
        "Int64", checks=[pa.Check.isin([0, 1])], nullable=True, coerce=True, required=False
    )

    for meta in contract.metadata_fields:
        if meta.name == contract.primary_key:
            continue
        columns[meta.name] = _column_for(meta, nullable=meta.name not in required_non_null)

    return pa.DataFrameSchema(
        columns=columns,
        coerce=True,
        strict=False,  # extra operational columns such as ingested_at are welcome
        name=f"raw_events@{contract.version}",
    )


# Backwards-compatible alias used by earlier call sites.
def get_raw_events_schema() -> pa.DataFrameSchema:
    return build_raw_events_schema()


@dataclass
class QualityIssue:
    rule: str
    column: str
    observed: float | int | str
    threshold: float | int | str
    message: str

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "column": self.column,
            "observed": self.observed,
            "threshold": self.threshold,
            "message": self.message,
        }


@dataclass
class QualityReport:
    """Batch-level data-quality verdict, complementing the per-row Pandera checks."""

    row_count: int
    issues: list[QualityIssue] = field(default_factory=list)
    null_fractions: dict[str, float] = field(default_factory=dict)
    duplicate_key_fraction: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "row_count": self.row_count,
            "passed": self.passed,
            "duplicate_key_fraction": round(self.duplicate_key_fraction, 6),
            "null_fractions": {k: round(v, 6) for k, v in self.null_fractions.items() if v > 0},
            "issues": [issue.as_dict() for issue in self.issues],
        }


def check_batch_quality(df: pd.DataFrame, contract: FeatureContract | None = None) -> QualityReport:
    """Apply the contract's batch-level rules (volume, null rate, duplicate keys)."""
    contract = contract or load_feature_contract()
    report = QualityReport(row_count=len(df))

    if len(df) < contract.min_rows:
        report.issues.append(
            QualityIssue(
                rule="min_rows",
                column="*",
                observed=len(df),
                threshold=contract.min_rows,
                message=f"Batch has {len(df)} rows, contract requires at least {contract.min_rows}",
            )
        )

    for name in contract.feature_names:
        if name not in df.columns:
            report.issues.append(
                QualityIssue(
                    rule="missing_column",
                    column=name,
                    observed="absent",
                    threshold="present",
                    message=f"Contract feature '{name}' is missing from the batch",
                )
            )
            continue
        null_fraction = float(df[name].isna().mean())
        report.null_fractions[name] = null_fraction
        if null_fraction > contract.max_null_fraction:
            report.issues.append(
                QualityIssue(
                    rule="max_null_fraction",
                    column=name,
                    observed=round(null_fraction, 4),
                    threshold=contract.max_null_fraction,
                    message=(
                        f"{null_fraction:.2%} of '{name}' is null, above the "
                        f"{contract.max_null_fraction:.2%} the contract tolerates"
                    ),
                )
            )

    for name in contract.required_non_null:
        if name in df.columns and df[name].isna().any():
            report.issues.append(
                QualityIssue(
                    rule="required_non_null",
                    column=name,
                    observed=int(df[name].isna().sum()),
                    threshold=0,
                    message=f"'{name}' must never be null",
                )
            )

    if contract.primary_key in df.columns and len(df):
        duplicates = int(df[contract.primary_key].duplicated().sum())
        report.duplicate_key_fraction = duplicates / len(df)
        if report.duplicate_key_fraction > contract.max_duplicate_key_fraction:
            report.issues.append(
                QualityIssue(
                    rule="max_duplicate_key_fraction",
                    column=contract.primary_key,
                    observed=duplicates,
                    threshold=contract.max_duplicate_key_fraction,
                    message=f"{duplicates} duplicate {contract.primary_key} values in one batch",
                )
            )

    return report
