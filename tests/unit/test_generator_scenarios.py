"""
Golden tests for the drift scenarios (Person 1, Phase E).

The contract these tests defend: injecting a scenario must move exactly the
features its YAML entry promises, far enough to cross the policy threshold, and
injecting nothing must leave the platform silent.
"""

from __future__ import annotations

import numpy as np
import pytest
from common.contracts import load_feature_contract
from distributions import DistributionProfile
from generator import generate_baseline_batch, run_generator
from metrics import calculate_js_distance, calculate_psi
from scenarios import ScenarioError, apply_scenario

PSI_CRITICAL = 0.20
JS_CRITICAL = 0.15
ROWS = 1500


@pytest.fixture
def reference(profile):
    return generate_baseline_batch(
        3000, "2026-09-01", "b-ref-001", "tr-ref-001", profile=profile, rng=np.random.default_rng(1)
    )


@pytest.fixture
def batch(profile):
    return generate_baseline_batch(
        ROWS, "2026-10-01", "b-cur-001", "tr-cur-001", profile=profile, rng=np.random.default_rng(2)
    )


def drift_score(reference, current, feature: str) -> float:
    contract = load_feature_contract()
    if feature in contract.numeric_features:
        return calculate_psi(reference[feature], current[feature])
    distance, _ = calculate_js_distance(reference[feature], current[feature])
    return distance


def threshold_for(feature: str) -> float:
    return PSI_CRITICAL if feature in load_feature_contract().numeric_features else JS_CRITICAL


# ----------------------------------------------------------------------
def test_catalogue_is_well_formed(catalogue):
    assert "none" in catalogue.scenarios
    for scenario in catalogue.scenarios.values():
        assert scenario.description, f"{scenario.name} needs a description"
        for operation in scenario.operations:
            assert "feature" in operation and "type" in operation
            if operation["type"] in ("multiplicative_shift", "additive_shift", "noise_scale", "label_odds_shift"):
                assert "magnitude" in operation, f"{scenario.name}:{operation['feature']} needs a magnitude"
            if operation["type"] in ("category_shift", "new_category"):
                assert "value" in operation


def test_no_drift_scenario_stays_silent(reference, batch, catalogue):
    drifted, audit = apply_scenario(batch, catalogue.get("none"), severity=1.0)
    assert audit == []
    for feature in load_feature_contract().numeric_features:
        assert calculate_psi(reference[feature], drifted[feature]) < PSI_CRITICAL


def test_zero_severity_is_a_no_op(batch, catalogue):
    drifted, audit = apply_scenario(batch, catalogue.get("price_hike"), severity=0.0)
    assert audit == []
    assert drifted["monthly_charges"].equals(batch["monthly_charges"])


@pytest.mark.parametrize("scenario_name", ["price_hike", "competitor_launch", "seasonal_spike", "new_plan"])
def test_scenario_moves_its_declared_features(scenario_name, reference, batch, catalogue):
    scenario = catalogue.get(scenario_name)
    drifted, audit = apply_scenario(batch, scenario, severity=1.0, rng=np.random.default_rng(7))

    assert audit, f"{scenario_name} applied no operations"
    breached = [
        feature
        for feature in scenario.expected_drift
        if drift_score(reference, drifted, feature) >= threshold_for(feature)
    ]
    assert breached, (
        f"{scenario_name} did not push any of {scenario.expected_drift} past its threshold: "
        + ", ".join(f"{f}={drift_score(reference, drifted, f):.3f}" for f in scenario.expected_drift)
    )


def test_price_hike_tops_the_drift_ranking(reference, batch, catalogue):
    """The demo's definition of done: monthly_charges must be the top drifted feature."""
    drifted, _ = apply_scenario(batch, catalogue.get("price_hike"), severity=0.8, rng=np.random.default_rng(3))
    scores = {
        feature: calculate_psi(reference[feature], drifted[feature])
        for feature in load_feature_contract().numeric_features
    }
    assert max(scores, key=scores.get) == "monthly_charges"
    assert scores["monthly_charges"] >= PSI_CRITICAL


def test_severity_scales_the_shift(reference, batch, catalogue):
    scenario = catalogue.get("price_hike")
    low, _ = apply_scenario(batch, scenario, severity=0.2, rng=np.random.default_rng(4))
    high, _ = apply_scenario(batch, scenario, severity=1.0, rng=np.random.default_rng(4))
    assert low["monthly_charges"].mean() < high["monthly_charges"].mean()
    assert calculate_psi(reference["monthly_charges"], low["monthly_charges"]) < calculate_psi(
        reference["monthly_charges"], high["monthly_charges"]
    )


def test_new_plan_introduces_an_unseen_category(batch, catalogue):
    drifted, _ = apply_scenario(batch, catalogue.get("new_plan"), severity=1.0, rng=np.random.default_rng(5))
    assert "Flexible-Monthly" in set(drifted["contract"].unique())
    assert "Flexible-Monthly" not in (load_feature_contract().allowed_values("contract") or [])


def test_corrupt_batch_breaks_the_contract_on_purpose(batch, catalogue):
    drifted, _ = apply_scenario(batch, catalogue.get("corrupt_batch"), severity=1.0, rng=np.random.default_rng(6))
    assert drifted["tenure"].min() < 0
    assert drifted["monthly_charges"].isna().any()


def test_label_rate_moves_with_the_business_story(batch, catalogue):
    drifted, _ = apply_scenario(batch, catalogue.get("price_hike"), severity=1.0, rng=np.random.default_rng(8))
    assert drifted["churn"].mean() > batch["churn"].mean()


def test_unknown_scenario_is_rejected(catalogue):
    with pytest.raises(ScenarioError):
        catalogue.get("meteor_strike")


# ----------------------------------------------------------------------
def test_generator_withholds_labels_and_releases_them_later(workspace):
    """Labels for batch d only become visible once batch d+14 is generated."""
    from common.warehouse import Warehouse

    landing = workspace["landing"]
    first = run_generator(scenario="none", date_str="2026-09-01", batch_size=200, output_dir=landing)

    import pandas as pd

    landed = pd.read_parquet(first["parquet_path"])
    assert landed["churn"].isna().all(), "labels must not land with the batch"
    assert first["released_labels"] == []

    warehouse = Warehouse.default(root=workspace["warehouse"])
    assert warehouse.read_table("labels").empty

    later = run_generator(scenario="none", date_str="2026-09-16", batch_size=200, output_dir=landing)
    assert [r["batch_id"] for r in later["released_labels"]] == [first["batch_id"]]
    released = warehouse.read_table("labels")
    assert len(released) == 200
    assert set(released["batch_id"]) == {first["batch_id"]}


def test_generator_is_deterministic_for_a_fixed_batch_id(profile):
    first = generate_baseline_batch(300, "2026-10-01", "b-fixed", "tr-fixed", profile=profile)
    second = generate_baseline_batch(300, "2026-10-01", "b-fixed", "tr-fixed", profile=profile)
    assert first["monthly_charges"].tolist() == second["monthly_charges"].tolist()


def test_profile_fit_round_trips(tmp_path, profile):
    frame = generate_baseline_batch(800, "2026-10-01", "b-fit", "tr-fit", profile=profile)
    fitted = DistributionProfile.fit(frame, source="test")
    path = fitted.save(str(tmp_path / "profile.json"))
    reloaded = DistributionProfile.load(path)

    assert reloaded.row_count == 800
    assert set(reloaded.numeric) == set(fitted.numeric)
    assert reloaded.churn_model["coefficients"].keys() == fitted.churn_model["coefficients"].keys()


def test_service_columns_stay_logically_consistent(profile):
    frame = generate_baseline_batch(1000, "2026-10-01", "b-consistent", "tr-c", profile=profile)
    no_internet = frame[frame["internet_service"] == "No"]
    assert (no_internet["online_security"] == "No internet service").all()
    no_phone = frame[frame["phone_service"] == "No"]
    assert (no_phone["multiple_lines"] == "No phone service").all()
