"""
Publish a sample event so a consumer can be built before its producer exists
(PRD §10.2 "mocks until then").

The payload is validated against contracts/events/<type>.json before it is sent,
so a mock can never be shaped differently from the real thing. Without GCP
credentials it lands in the local outbox; with CHURNOPS_USE_PUBSUB=1 it goes to
the real topic.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))

from common.contracts import contracts_dir  # noqa: E402
from common.events import EventPublisher, build_event, validate_event  # noqa: E402

SAMPLE_DIR = os.path.join(contracts_dir(), "events", "examples")

DEFAULTS = {
    "data.batch.landed": lambda: build_event(
        "data.batch.landed",
        trace_id="tr-mock-0001",
        batch_id="b-20261017-mock",
        batch_date=datetime.date.today().isoformat(),
        row_count=1500,
        gcs_uri="gs://churnops-dev-landing/batches/date=2026-10-17/batch_b-20261017-mock.parquet",
    ),
    "drift.detected": lambda: build_event(
        "drift.detected",
        trace_id="tr-mock-0001",
        batch_id="b-20261017-mock",
        batch_date=datetime.date.today().isoformat(),
        severity="critical",
        share_of_drifted_features=0.35,
        drifted_features=[
            {"feature": "monthly_charges", "metric": "psi", "score": 0.284, "threshold": 0.20},
            {"feature": "contract", "metric": "jensen_shannon", "score": 0.195, "threshold": 0.15},
        ],
        prediction_drift_p_value=0.003,
        report_uri="gs://churnops-dev-reports/drift/b-20261017-mock/report.html",
    ),
}


def publish_fake_event(event_type: str, out_dir: str | None = None) -> dict:
    sample_path = os.path.join(SAMPLE_DIR, f"{event_type}.sample.json")
    if os.path.exists(sample_path):
        with open(sample_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        validate_event(event_type, payload)
    elif event_type in DEFAULTS:
        payload = DEFAULTS[event_type]()
    else:
        raise SystemExit(
            f"No sample for '{event_type}'. Add contracts/events/examples/{event_type}.sample.json"
        )

    publisher = EventPublisher(outbox_dir=out_dir or EventPublisher.outbox_dir, service="mock-publisher")
    result = publisher.publish(event_type, payload, trace_id=payload.get("trace_id"))
    print(json.dumps({"event_type": event_type, **{k: v for k, v in result.items() if k != "payload"}}, indent=2))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publish a contract-valid mock event")
    parser.add_argument("--event-type", default="drift.detected", help="e.g. data.batch.landed, drift.detected")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()
    publish_fake_event(args.event_type, args.out_dir)
