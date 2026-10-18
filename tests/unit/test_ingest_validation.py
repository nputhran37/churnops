"""
Ingestion and validation tests (Person 1, Phase E).

Guarantees under test: a clean batch lands exactly once no matter how often the
job reruns, a bad batch never reaches the warehouse, and the quarantine artefacts
contain enough detail to work the runbook.
"""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest
from common.events import read_outbox, validate_event
from common.warehouse import Warehouse
from generator import run_generator
from ingest import process_batch
from schemas import build_raw_events_schema, check_batch_quality


def land(workspace, scenario="none", severity=0.0, date="2026-10-01", rows=300):
    return run_generator(
        scenario=scenario,
        severity=severity,
        date_str=date,
        batch_size=rows,
        output_dir=workspace["landing"],
    )


def ingest(workspace, batch):
    return process_batch(
        batch_file_path=batch["parquet_path"],
        quarantine_dir=workspace["quarantine"],
        warehouse_dir=workspace["warehouse"],
        events_dir=workspace["outbox"],
    )


# ----------------------------------------------------------------------
def test_clean_batch_loads_and_announces_itself(workspace):
    batch = land(workspace)
    result = ingest(workspace, batch)

    assert result["status"] == "success"
    assert result["row_count"] == 300
    assert os.path.exists(result["raw_path"])
    assert os.path.exists(result["features_path"])

    events = read_outbox("data.batch.landed", workspace["outbox"])
    assert len(events) == 1
    validate_event("data.batch.landed", events[0])
    assert events[0]["batch_id"] == batch["batch_id"]
    assert events[0]["trace_id"] == batch["trace_id"]
    assert events[0]["gcs_uri"].startswith("gs://")


def test_rerunning_the_same_batch_does_not_duplicate_rows(workspace):
    batch = land(workspace)
    first = ingest(workspace, batch)
    second = ingest(workspace, batch)

    assert first["load_mode"] == "inserted"
    assert second["load_mode"] == "replaced"

    warehouse = Warehouse.default(root=workspace["warehouse"])
    raw = warehouse.read_table("raw_events")
    assert len(raw) == 300
    assert raw["customer_id"].duplicated().sum() == 0


def test_features_daily_matches_the_bigquery_contract(workspace):
    from common.contracts import bq_column_names

    batch = land(workspace)
    result = ingest(workspace, batch)
    features = pd.read_parquet(result["features_path"])
    assert list(features.columns) == bq_column_names("features_daily")


def test_corrupt_batch_is_quarantined_and_never_reaches_the_warehouse(workspace):
    batch = land(workspace, scenario="corrupt_batch", severity=1.0, rows=400)
    result = ingest(workspace, batch)

    assert result["status"] == "quarantined"
    assert os.path.exists(result["quarantine_path"])
    assert result["quarantine_uri"].startswith("gs://")

    with open(result["error_path"], "r", encoding="utf-8") as handle:
        error = json.load(handle)
    assert error["batch_id"] == batch["batch_id"]
    assert error["runbook"].endswith("batch_quarantined.md")
    rules = {issue["rule"] for issue in error["issues"]}
    assert {"pandera_check", "max_null_fraction"} & rules

    warehouse = Warehouse.default(root=workspace["warehouse"])
    assert warehouse.read_table("raw_events").empty
    assert read_outbox("data.batch.landed", workspace["outbox"]) == []


def test_unseen_category_is_quarantined_until_the_contract_is_bumped(workspace):
    batch = land(workspace, scenario="new_plan", severity=1.0, rows=400)
    result = ingest(workspace, batch)

    assert result["status"] == "quarantined"
    columns = {issue["column"] for issue in result["issues"]}
    assert "contract" in columns


def test_delayed_labels_are_attached_when_they_arrive(workspace):
    first = land(workspace, date="2026-09-01", rows=200)
    ingest(workspace, first)

    # 14 days later the generator releases the first batch's labels...
    land(workspace, date="2026-09-16", rows=200)

    # ...and re-ingesting the original batch now fills its churn column.
    result = ingest(workspace, first)
    assert result["labelled_rows"] == 200
    raw = pd.read_parquet(result["raw_path"])
    assert raw["churn"].notna().all()


def test_small_batch_fails_the_volume_rule(workspace, contract):
    batch = land(workspace, rows=20)
    result = ingest(workspace, batch)
    assert result["status"] == "quarantined"
    assert any(issue["rule"] == "min_rows" for issue in result["issues"])


def test_duplicate_customer_ids_are_rejected(workspace, contract):
    batch = land(workspace, rows=200)
    frame = pd.read_parquet(batch["parquet_path"])
    frame.loc[frame.index[:10], "customer_id"] = frame["customer_id"].iloc[0]
    path = os.path.join(workspace["landing"], "duplicated.parquet")
    frame.to_parquet(path, index=False)

    result = process_batch(
        batch_file_path=path,
        quarantine_dir=workspace["quarantine"],
        warehouse_dir=workspace["warehouse"],
        events_dir=workspace["outbox"],
    )
    assert result["status"] == "quarantined"
    rules = {issue["rule"] for issue in result["issues"]}
    assert "max_duplicate_key_fraction" in rules or "pandera_check" in rules


def test_quality_report_counts_nulls_without_failing_rare_ones(workspace, contract):
    batch = land(workspace, rows=300)
    frame = pd.read_parquet(batch["parquet_path"])
    frame.loc[frame.index[:2], "monthly_charges"] = None  # 0.7% < 2% tolerance

    report = check_batch_quality(frame, contract)
    assert report.passed
    assert report.null_fractions["monthly_charges"] > 0


def test_schema_is_generated_from_the_contract(contract):
    schema = build_raw_events_schema(contract)
    for feature in contract.feature_names:
        assert feature in schema.columns
    assert schema.columns[contract.primary_key].unique is True


def test_cloudevent_payload_resolves_the_object_uri():
    from ingest import batch_path_from_cloudevent

    uri = batch_path_from_cloudevent(
        {"data": {"bucket": "churnops-dev-landing", "name": "batches/date=2026-10-01/batch_b-1.parquet"}}
    )
    assert uri == "gs://churnops-dev-landing/batches/date=2026-10-01/batch_b-1.parquet"

    with pytest.raises(ValueError):
        batch_path_from_cloudevent({"data": {}})
