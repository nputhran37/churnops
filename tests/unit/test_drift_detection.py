"""
Drift detection, policy and cooldown tests (Person 1, Phase E).

The definition of done for P1: `make inject-drift SCENARIO=price_hike` produces a
drift.detected event whose report names monthly_charges as the top drifted
feature - and a clean day produces silence.
"""

from __future__ import annotations

import datetime
import json
import os

import numpy as np
import pandas as pd
import pytest
from common.events import read_outbox, validate_event
from common.warehouse import Warehouse
from detector import cooldown_state, decide_severity, load_policy, run_drift_analysis
from generator import generate_baseline_batch, run_generator
from ingest import process_batch
from metrics import FeatureDrift, calculate_psi, chi_square_p_value, score_categorical_feature, score_numeric_feature
from reference import ReferenceWindow, refresh_on_model_promoted


@pytest.fixture
def seeded(workspace, profile):
    """A warehouse with history, a frozen reference window and no incidents yet."""
    warehouse = Warehouse.default(root=workspace["warehouse"])
    history = generate_baseline_batch(2500, "2026-09-15", "b-hist-001", "tr-hist", profile=profile)
    warehouse.merge_batch("raw_events", history, batch_id="b-hist-001", batch_date="2026-09-15")
    ReferenceWindow.build(
        frame=history,
        model_version="v3",
        snapshot_date="2026-09-15",
        warehouse=warehouse,
    )
    return workspace


def analyse(workspace, batch, **kwargs):
    return run_drift_analysis(
        current_batch_path=batch["parquet_path"],
        reports_dir=workspace["reports"],
        events_dir=workspace["outbox"],
        warehouse_dir=workspace["warehouse"],
        **kwargs,
    )


def land(workspace, scenario, severity, date="2026-10-01", rows=1200):
    return run_generator(
        scenario=scenario, severity=severity, date_str=date, batch_size=rows, output_dir=workspace["landing"]
    )


# ----------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------
def test_psi_is_zero_for_identical_samples():
    values = np.random.default_rng(0).normal(60, 10, 2000)
    assert calculate_psi(values, values) == pytest.approx(0.0, abs=1e-6)


def test_psi_grows_with_the_shift():
    rng = np.random.default_rng(1)
    reference = rng.normal(60, 10, 3000)
    small = calculate_psi(reference, reference * 1.05)
    large = calculate_psi(reference, reference * 1.30)
    assert small < large
    assert large > 0.2


def test_chi_square_flags_a_changed_category_mix():
    reference = pd.Series(["A"] * 700 + ["B"] * 300)
    same = pd.Series(["A"] * 350 + ["B"] * 150)
    shifted = pd.Series(["A"] * 200 + ["B"] * 300)
    assert chi_square_p_value(reference, same) > 0.05
    assert chi_square_p_value(reference, shifted) < 0.01


def test_unseen_category_is_always_critical():
    reference = pd.Series(["Month-to-month"] * 500 + ["One year"] * 300)
    current = pd.Series(["Month-to-month"] * 400 + ["Flexible-Monthly"] * 50)
    result = score_categorical_feature("contract", reference, current, 0.08, 0.15)
    assert result.severity == "critical"
    assert result.detail["unseen_categories"] == ["Flexible-Monthly"]


def test_numeric_scorer_reports_the_direction_of_the_change():
    rng = np.random.default_rng(3)
    reference = pd.Series(rng.normal(60, 8, 2000))
    current = pd.Series(rng.normal(75, 8, 2000))
    result = score_numeric_feature("monthly_charges", reference, current, 0.10, 0.20)
    assert result.severity == "critical"
    assert result.detail["current_mean"] > result.detail["reference_mean"]
    assert result.detail["ks_p_value"] < 0.01


# ----------------------------------------------------------------------
# policy
# ----------------------------------------------------------------------
def drift(feature, severity, score=0.5):
    return FeatureDrift(
        feature=feature,
        type="numeric",
        metric="psi",
        score=score,
        threshold=0.2,
        severity=severity,
        is_drifted=severity != "none",
    )


def test_top_priority_feature_alone_escalates_to_critical():
    policy = load_policy()
    severity, reasons = decide_severity([drift("monthly_charges", "critical"), drift("gender", "none", 0.01)],
                                        None, policy, row_count=1000)
    assert severity == "critical"
    assert "monthly_charges" in reasons[0]


def test_many_small_drifts_escalate_by_share():
    policy = load_policy()
    results = [drift(f"f{i}", "warn", 0.12) for i in range(4)] + [drift(f"g{i}", "none", 0.01) for i in range(6)]
    severity, _ = decide_severity(results, None, policy, row_count=1000)
    assert severity == "critical"


def test_prediction_drift_alone_escalates():
    policy = load_policy()
    severity, reasons = decide_severity([drift("gender", "none", 0.01)], 0.0001, policy, row_count=1000)
    assert severity == "critical"
    assert "prediction" in reasons[-1]


def test_clean_batch_is_none():
    policy = load_policy()
    severity, reasons = decide_severity([drift(f"f{i}", "none", 0.01) for i in range(8)], 0.6, policy, 1000)
    assert severity == "none"
    assert reasons == ["all monitored features within thresholds"]


def test_small_batch_never_triggers_a_retrain():
    policy = load_policy()
    severity, reasons = decide_severity([drift("monthly_charges", "critical")], 0.0001, policy, row_count=10)
    assert severity == "none"
    assert "min_rows_for_verdict" in reasons[0]


def test_cooldown_suppresses_a_second_incident(workspace):
    policy = load_policy()
    warehouse = Warehouse.default(root=workspace["warehouse"])
    now = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)

    assert cooldown_state(warehouse, policy, "critical", now)["suppressed"] is False

    warehouse.insert_row(
        "drift_reports",
        {
            "report_id": "rep-1",
            "batch_id": "b-1",
            "batch_date": "2026-10-01",
            "trace_id": "tr-1",
            "severity": "critical",
            "dataset_drift_detected": True,
            "share_of_drifted_features": 0.4,
            "top_drifted_features": "[]",
            "gcs_report_html_uri": "gs://x/report.html",
            "gcs_report_json_uri": "gs://x/report.json",
            "created_at": (now - datetime.timedelta(hours=1)).isoformat(),
            "published_event": True,
        },
    )

    within = cooldown_state(warehouse, policy, "critical", now)
    assert within["suppressed"] is True
    assert within["hours_since_last_incident"] == pytest.approx(1.0, abs=0.01)

    later = cooldown_state(warehouse, policy, "critical", now + datetime.timedelta(hours=5))
    assert later["suppressed"] is False

    # A warn-level verdict is never gated by the cooldown.
    assert cooldown_state(warehouse, policy, "warn", now)["suppressed"] is False


# ----------------------------------------------------------------------
# end to end through the detector
# ----------------------------------------------------------------------
def test_price_hike_emits_drift_detected_naming_monthly_charges(seeded):
    batch = land(seeded, "price_hike", 0.8)
    result = analyse(seeded, batch)

    assert result["severity"] == "critical"
    assert result["event_payload"] is not None
    validate_event("drift.detected", result["event_payload"])
    assert result["event_payload"]["drifted_features"][0]["feature"] == "monthly_charges"
    assert result["event_payload"]["trace_id"] == batch["trace_id"]

    published = read_outbox("drift.detected", seeded["outbox"])
    assert len(published) == 1

    with open(result["report_json"], "r", encoding="utf-8") as handle:
        report = json.load(handle)
    assert report["features"][0]["feature"] == "monthly_charges"
    assert report["reference"]["model_version"] == "v3"
    assert os.path.exists(result["report_html"])

    rows = Warehouse.default(root=seeded["warehouse"]).read_rows("drift_reports")
    assert rows[-1]["batch_id"] == batch["batch_id"]
    assert rows[-1]["severity"] == "critical"


def test_clean_batch_stays_silent(seeded):
    batch = land(seeded, "none", 0.0)
    result = analyse(seeded, batch)

    assert result["severity"] == "none"
    assert result["event_payload"] is None
    assert read_outbox("drift.detected", seeded["outbox"]) == []
    # The quiet run is still recorded, which is what the dashboard timeline plots.
    assert Warehouse.default(root=seeded["warehouse"]).read_rows("drift_reports")


def test_second_drift_within_the_cooldown_is_not_published(seeded):
    first = land(seeded, "price_hike", 0.8, date="2026-10-01")
    assert analyse(seeded, first)["event_payload"] is not None

    second = land(seeded, "price_hike", 0.9, date="2026-10-02")
    result = analyse(seeded, second)

    assert result["severity"] == "critical"
    assert result["cooldown"]["suppressed"] is True
    assert result["event_payload"] is None
    assert len(read_outbox("drift.detected", seeded["outbox"])) == 1


def test_model_promoted_refreshes_the_reference_window(seeded):
    warehouse = Warehouse.default(root=seeded["warehouse"])
    before = ReferenceWindow.load(warehouse)
    assert before.model_version == "v3"

    batch = land(seeded, "price_hike", 0.8)
    process_batch(
        batch_file_path=batch["parquet_path"],
        quarantine_dir=seeded["quarantine"],
        warehouse_dir=seeded["warehouse"],
        events_dir=seeded["outbox"],
    )

    result = refresh_on_model_promoted(
        {"event_type": "model.promoted", "promoted_model_version": "v4", "trace_id": "tr-promo"},
        warehouse,
    )
    assert result["status"] == "refreshed"

    after = ReferenceWindow.load(warehouse)
    assert after.model_version == "v4"
    assert after.frame["monthly_charges"].mean() > before.frame["monthly_charges"].mean()

    # The drifted world is now part of normal, so the same batch looks far less extreme.
    before_psi = calculate_psi(before.frame["monthly_charges"], pd.read_parquet(batch["parquet_path"])["monthly_charges"])
    after_psi = calculate_psi(after.frame["monthly_charges"], pd.read_parquet(batch["parquet_path"])["monthly_charges"])
    assert after_psi < before_psi / 2


def test_reference_window_rows_match_the_bigquery_contract(seeded):
    from common.contracts import bq_column_names

    rows = Warehouse.default(root=seeded["warehouse"]).read_rows("reference_window")
    allowed = set(bq_column_names("reference_window"))
    assert rows
    for row in rows:
        assert set(row).issubset(allowed)
    numeric = next(r for r in rows if r["feature_name"] == "monthly_charges")
    assert json.loads(numeric["quantiles"])["q50"] > 0
    categorical = next(r for r in rows if r["feature_name"] == "contract")
    assert sum(json.loads(categorical["category_distribution"]).values()) == pytest.approx(1.0, abs=0.01)


def test_detector_skips_cleanly_without_a_reference_window(workspace):
    batch = land(workspace, "price_hike", 0.8)
    result = analyse(workspace, batch)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_reference_window"


def test_realised_performance_backfills_from_delayed_labels(seeded):
    from realised_performance import backfill_all

    batch = land(seeded, "price_hike", 0.8, date="2026-10-01")
    process_batch(
        batch_file_path=batch["parquet_path"],
        quarantine_dir=seeded["quarantine"],
        warehouse_dir=seeded["warehouse"],
        events_dir=seeded["outbox"],
    )
    analyse(seeded, batch)

    waiting = backfill_all(warehouse_dir=seeded["warehouse"])
    assert all(detail["status"] in ("waiting", "skipped") for detail in waiting["details"])

    land(seeded, "none", 0.0, date="2026-10-16", rows=300)  # releases the 2026-10-01 labels
    done = backfill_all(warehouse_dir=seeded["warehouse"])

    updated = [d for d in done["details"] if d["status"] == "updated"]
    assert updated, done
    assert 0.0 <= updated[0]["realised_auc"] <= 1.0

    row = next(
        r for r in Warehouse.default(root=seeded["warehouse"]).read_rows("drift_reports")
        if r["batch_id"] == batch["batch_id"]
    )
    assert row["realised_auc"] is not None
    assert row["realised_label_count"] > 0
