"""
Contract-parity tests (Person 1, Phase E).

features.yaml is the hand-off to P2, so these tests assert that everything derived
from it stays in step: the warehouse schema, the Python transform, its generated
BigQuery twin, the Pandera rules, and the payloads the data plane puts on the bus.
"""

from __future__ import annotations

import glob
import json
import os
import re

import jsonschema
import pandas as pd
import pytest
from common.contracts import bq_column_names, load_event_schema
from common.events import EventValidationError, build_event, validate_event
from common.features import FeatureTransformer, load_feature_stats
from generator import generate_baseline_batch
from schemas import build_raw_events_schema

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


@pytest.fixture
def batch(profile):
    return generate_baseline_batch(400, "2026-10-01", "b-parity", "tr-parity", profile=profile)


# ----------------------------------------------------------------------
# contract -> warehouse
# ----------------------------------------------------------------------
def test_features_daily_columns_are_derived_from_the_contract(contract):
    derived = (
        ["customer_id"]
        + [spec.output_column for spec in contract.features]
        + [contract.target_name, "customer_segment", "batch_date", "batch_id", "trace_id"]
    )
    assert derived == bq_column_names("features_daily")


def test_raw_events_schema_covers_every_contract_column(contract):
    columns = set(bq_column_names("raw_events"))
    for name in contract.raw_columns:
        assert name in columns, f"{name} is in features.yaml but not contracts/bq/raw_events.json"


def test_every_bq_schema_is_well_formed():
    for path in glob.glob(os.path.join(PROJECT_ROOT, "contracts", "bq", "*.json")):
        with open(path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        assert isinstance(schema, list) and schema, path
        for field in schema:
            assert {"name", "type"} <= set(field), f"{path}: {field}"
            assert field.get("mode", "NULLABLE") in ("REQUIRED", "NULLABLE", "REPEATED")


def test_labels_table_is_contracted():
    assert "churn" in bq_column_names("labels")
    assert {"customer_id", "batch_id", "label_available_at"} <= set(bq_column_names("labels"))


# ----------------------------------------------------------------------
# python transform <-> generated SQL
# ----------------------------------------------------------------------
def test_transform_output_matches_the_contract(batch):
    transformer = FeatureTransformer.from_contracts()
    features = transformer.transform(batch)
    assert list(features.columns) == bq_column_names("features_daily")
    assert len(features) == len(batch)


def test_binary_and_scaling_transforms_follow_the_spec(batch, contract):
    transformer = FeatureTransformer.from_contracts()
    features = transformer.transform(batch)
    stats = load_feature_stats()

    assert set(features["partner_encoded"].unique()) <= {0, 1}
    expected = round((batch["monthly_charges"].iloc[0] - stats["monthly_charges"]["mean"]) /
                     stats["monthly_charges"]["std"], 4)
    assert features["monthly_charges_scaled"].iloc[0] == pytest.approx(expected, abs=1e-4)

    tenure_spec = contract.get("tenure")
    span = tenure_spec.maximum - tenure_spec.minimum
    assert features["tenure_scaled"].iloc[0] == pytest.approx(batch["tenure"].iloc[0] / span, abs=1e-4)


def test_generated_sql_implements_the_same_spec(contract):
    transformer = FeatureTransformer.from_contracts()
    sql = transformer.to_sql("proj.churnops.raw_events", "proj.churnops.features_daily")

    assert "MERGE" in sql and "WHEN MATCHED THEN UPDATE SET" in sql
    assert "@batch_id" in sql, "the merge must be scoped to one batch for idempotency"

    for column in bq_column_names("features_daily"):
        assert re.search(rf"\b{re.escape(column)}\b", sql), f"{column} missing from generated SQL"

    stats = load_feature_stats()
    assert str(stats["monthly_charges"]["mean"]) in sql
    assert str(stats["monthly_charges"]["std"]) in sql
    # Statistical impute rules must be resolved to literals, never leaked as words.
    assert "'mode'" not in sql and "'median'" not in sql


def test_sql_and_python_agree_on_every_transform_type(contract):
    transformer = FeatureTransformer.from_contracts()
    sql = transformer.to_sql("p.d.raw_events", "p.d.features_daily")
    for spec in contract.features:
        if spec.transform == "binary_encode":
            assert f"= 'Yes' THEN 1 ELSE 0 END AS {spec.output_column}" in sql
        if spec.transform == "log1p":
            assert f"LN(1 + IFNULL(CAST(`{spec.name}`" in sql


# ----------------------------------------------------------------------
# generated data <-> validation rules
# ----------------------------------------------------------------------
def test_generated_batch_validates_against_the_contract(batch, contract):
    build_raw_events_schema(contract).validate(batch, lazy=True)


def test_generated_values_stay_inside_declared_ranges(batch, contract):
    for spec in contract.features:
        if spec.allowed_values is not None:
            unexpected = set(batch[spec.name].astype(str)) - {str(v) for v in spec.allowed_values}
            assert not unexpected, f"{spec.name} produced {unexpected}"
        if spec.minimum is not None:
            assert pd.to_numeric(batch[spec.name], errors="coerce").min() >= spec.minimum
        if spec.maximum is not None:
            assert pd.to_numeric(batch[spec.name], errors="coerce").max() <= spec.maximum


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------
def test_data_batch_landed_payload_matches_its_schema():
    event = build_event(
        "data.batch.landed",
        trace_id="tr-1",
        batch_id="b-1",
        batch_date="2026-10-01",
        row_count=1200,
        gcs_uri="gs://churnops-dev-landing/batches/date=2026-10-01/batch_b-1.parquet",
    )
    jsonschema.validate(event, load_event_schema("data.batch.landed"))


def test_drift_detected_payload_matches_its_schema():
    event = build_event(
        "drift.detected",
        trace_id="tr-1",
        batch_id="b-1",
        batch_date="2026-10-01",
        severity="critical",
        share_of_drifted_features=0.4,
        drifted_features=[{"feature": "monthly_charges", "metric": "psi", "score": 0.68, "threshold": 0.2}],
        prediction_drift_p_value=0.0001,
        report_uri="gs://churnops-dev-reports/drift/b-1/report.html",
    )
    jsonschema.validate(event, load_event_schema("drift.detected"))


def test_invalid_event_is_rejected_before_it_reaches_the_bus():
    with pytest.raises(EventValidationError):
        build_event("drift.detected", trace_id="tr-1", batch_id="b-1", severity="apocalyptic")

    with pytest.raises(EventValidationError):
        validate_event("data.batch.landed", {"event_type": "data.batch.landed"})


def test_sample_payloads_still_satisfy_their_schemas():
    for path in glob.glob(os.path.join(PROJECT_ROOT, "contracts", "events", "examples", "*.json")):
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        jsonschema.validate(payload, load_event_schema(payload["event_type"]))


def test_every_event_carries_the_envelope_fields():
    for path in glob.glob(os.path.join(PROJECT_ROOT, "contracts", "events", "*.json")):
        with open(path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        required = set(schema.get("required", []))
        assert {"event_type", "schema_version", "trace_id", "emitted_at"} <= required, path


# ----------------------------------------------------------------------
# logging contract
# ----------------------------------------------------------------------
def test_structured_logs_carry_the_required_fields(capsys):
    from common.logging_utils import get_logger

    logger = get_logger("drift-detector")
    payload = logger.warning("Feature drift detected", "tr-log-1", batch_id="b-1", metrics={"psi": 0.28})

    assert {"timestamp", "severity", "service", "message", "trace_id"} <= set(payload)
    assert payload["severity"] == "WARNING"
    assert json.loads(json.dumps(payload))["metrics"]["psi"] == 0.28


def test_committed_features_sql_matches_the_generator():
    """The checked-in BigQuery transform must be regenerated whenever the contract moves."""
    path = os.path.join(PROJECT_ROOT, "data", "ingest", "sql", "features_daily.sql")
    if not os.path.exists(path):
        pytest.skip("run `make seed` to generate data/ingest/sql/features_daily.sql")

    from common.warehouse import Warehouse

    warehouse = Warehouse.default()
    expected = FeatureTransformer.from_contracts().to_sql(
        f"{warehouse.project}.{warehouse.dataset}.raw_events",
        f"{warehouse.project}.{warehouse.dataset}.features_daily",
    )
    with open(path, "r", encoding="utf-8") as handle:
        committed = handle.read()
    assert committed.strip() == expected.strip(), (
        "data/ingest/sql/features_daily.sql is stale - rerun `make seed` after changing "
        "contracts/features.yaml or contracts/feature_stats.json"
    )
