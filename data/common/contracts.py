"""
Loader for contracts/features.yaml and the contracts/ directory in general.

features.yaml is the single hand-off between P1 (data) and P2 (ML/serving):
every validation rule, every transform and every drift feature list is derived
from it at runtime, so a contract change can never silently diverge from code.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

_ENV_CONTRACTS_DIR = "CHURNOPS_CONTRACTS_DIR"


def contracts_dir() -> str:
    """Resolve the contracts/ directory locally or inside a container."""
    explicit = os.getenv(_ENV_CONTRACTS_DIR)
    if explicit:
        return explicit

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.abspath(os.path.join(here, "..", "..", "contracts")),  # repo checkout
        os.path.abspath(os.path.join(here, "..", "contracts")),        # container: /app/contracts
        os.path.abspath(os.path.join(os.getcwd(), "contracts")),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError(
        "Could not locate the contracts/ directory. Set CHURNOPS_CONTRACTS_DIR."
    )


@dataclass(frozen=True)
class FeatureSpec:
    """One row of contracts/features.yaml -> features[]."""

    name: str
    dtype: str
    transform: str = "identity"
    impute: Any = None
    allowed_values: list | None = None
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    @property
    def is_numeric(self) -> bool:
        return self.dtype in ("int64", "float64") and not self.allowed_values

    @property
    def is_categorical(self) -> bool:
        return not self.is_numeric

    @property
    def output_column(self) -> str:
        """Name this feature carries in features_daily (contracts/bq/features_daily.json)."""
        suffix = {
            "binary_encode": "_encoded",
            "min_max_scale": "_scaled",
            "standard_scale": "_scaled",
            "log1p": "_log1p",
        }.get(self.transform, "")
        return f"{self.name}{suffix}"


@dataclass(frozen=True)
class FeatureContract:
    version: str
    dataset: str
    primary_key: str
    target: dict
    features: list[FeatureSpec]
    metadata_fields: list[FeatureSpec] = field(default_factory=list)
    validation: dict = field(default_factory=dict)

    # --- batch-level data-quality rules --------------------------------
    @property
    def min_rows(self) -> int:
        return int(self.validation.get("min_rows", 1))

    @property
    def max_null_fraction(self) -> float:
        return float(self.validation.get("max_null_fraction", 1.0))

    @property
    def max_duplicate_key_fraction(self) -> float:
        return float(self.validation.get("max_duplicate_key_fraction", 0.0))

    @property
    def required_non_null(self) -> list[str]:
        return list(self.validation.get("required_non_null", [self.primary_key]))

    @property
    def customer_id_pattern(self) -> str:
        return str(self.validation.get("customer_id_pattern", r"^CUST-[A-Z0-9]+$"))

    # --- lookups -------------------------------------------------------
    def get(self, name: str) -> FeatureSpec:
        for spec in self.features + self.metadata_fields:
            if spec.name == name:
                return spec
        raise KeyError(f"'{name}' is not defined in contracts/features.yaml")

    @property
    def feature_names(self) -> list[str]:
        return [f.name for f in self.features]

    @property
    def numeric_features(self) -> list[str]:
        return [f.name for f in self.features if f.is_numeric]

    @property
    def categorical_features(self) -> list[str]:
        return [f.name for f in self.features if f.is_categorical]

    @property
    def target_name(self) -> str:
        return self.target["name"]

    @property
    def raw_columns(self) -> list[str]:
        """Every column a landed batch must carry (features + target + metadata)."""
        return self.feature_names + [self.target_name] + [m.name for m in self.metadata_fields]

    def allowed_values(self, name: str) -> list | None:
        return self.get(name).allowed_values


def _to_spec(raw: dict) -> FeatureSpec:
    return FeatureSpec(
        name=raw["name"],
        dtype=raw["dtype"],
        transform=raw.get("transform", "identity"),
        impute=raw.get("impute"),
        allowed_values=raw.get("allowed_values"),
        minimum=raw.get("min"),
        maximum=raw.get("max"),
        description=raw.get("description", ""),
    )


@functools.lru_cache(maxsize=4)
def load_feature_contract(path: str | None = None) -> FeatureContract:
    """Parse contracts/features.yaml into typed specs (cached per path)."""
    path = path or os.path.join(contracts_dir(), "features.yaml")
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    return FeatureContract(
        version=raw["version"],
        dataset=raw["dataset"],
        primary_key=raw["primary_key"],
        target=raw["target"],
        features=[_to_spec(f) for f in raw["features"]],
        metadata_fields=[_to_spec(m) for m in raw.get("metadata_fields", [])],
        validation=raw.get("validation", {}),
    )


@functools.lru_cache(maxsize=16)
def load_bq_schema(table: str) -> list[dict]:
    """Load a BigQuery JSON schema from contracts/bq/<table>.json."""
    path = os.path.join(contracts_dir(), "bq", f"{table}.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=16)
def load_event_schema(event_type: str) -> dict:
    """Load a Pub/Sub JSON schema from contracts/events/<event.type>.json."""
    path = os.path.join(contracts_dir(), "events", f"{event_type}.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def bq_column_names(table: str) -> list[str]:
    return [field_def["name"] for field_def in load_bq_schema(table)]


def required_bq_columns(table: str) -> list[str]:
    return [f["name"] for f in load_bq_schema(table) if f.get("mode") == "REQUIRED"]


def iter_contract_paths(subdir: str, pattern: str = ".json") -> Iterable[str]:
    base = os.path.join(contracts_dir(), subdir)
    for entry in sorted(os.listdir(base)):
        if entry.endswith(pattern):
            yield os.path.join(base, entry)
