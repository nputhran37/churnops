"""
Shared pytest fixtures for the ChurnOps test suite.

Every test runs against an isolated warehouse/landing/report workspace in tmp_path,
so the unit tests never touch the developer's local data plane and can run in
parallel in CI.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for path in (
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "data"),
    os.path.join(PROJECT_ROOT, "data", "generator"),
    os.path.join(PROJECT_ROOT, "data", "ingest"),
    os.path.join(PROJECT_ROOT, "data", "drift"),
    os.path.join(PROJECT_ROOT, "scripts"),
):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("CHURNOPS_CONTRACTS_DIR", os.path.join(PROJECT_ROOT, "contracts"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated data plane: landing, quarantine, reports, warehouse and outbox."""
    layout = {
        "landing": tmp_path / "landing",
        "quarantine": tmp_path / "quarantine",
        "reports": tmp_path / "reports",
        "warehouse": tmp_path / "warehouse",
        "outbox": tmp_path / "events_outbox",
    }
    for directory in layout.values():
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CHURNOPS_LANDING_DIR", str(layout["landing"]))
    monkeypatch.setenv("CHURNOPS_QUARANTINE_DIR", str(layout["quarantine"]))
    monkeypatch.setenv("CHURNOPS_REPORTS_DIR", str(layout["reports"]))
    monkeypatch.setenv("CHURNOPS_WAREHOUSE_DIR", str(layout["warehouse"]))
    monkeypatch.setenv("CHURNOPS_USE_GCS", "0")
    monkeypatch.setenv("CHURNOPS_USE_BQ", "0")
    monkeypatch.setenv("CHURNOPS_USE_PUBSUB", "0")
    monkeypatch.setenv("CHURNOPS_PREDICT_URL", "")

    return {name: str(path) for name, path in layout.items()}


@pytest.fixture
def contract():
    from common.contracts import load_feature_contract

    return load_feature_contract()


@pytest.fixture
def profile():
    from distributions import DistributionProfile

    return DistributionProfile.builtin()


@pytest.fixture
def catalogue():
    from scenarios import ScenarioCatalogue

    return ScenarioCatalogue.load()
