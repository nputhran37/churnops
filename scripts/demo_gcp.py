"""Run a GCP-backed ChurnOps incident and let Cloud Run handle its events.

The generator and ingest client run from this workstation using Application
Default Credentials. Their storage, warehouse, and event transports are GCS,
BigQuery, and Pub/Sub. Pub/Sub then invokes the deployed drift and analyst
services and the Vertex pipeline trigger.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "generator"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "drift"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "ingest"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Run a GCP-backed ChurnOps incident demo")
    parser.add_argument("--project-id", default="churnops-512010")
    parser.add_argument("--scenario", default="price_hike")
    parser.add_argument("--severity", type=float, default=0.8)
    parser.add_argument("--batch-size", type=int, default=1500)
    args = parser.parse_args(argv)

    os.environ.update({
        "CHURNOPS_PROJECT_ID": args.project_id,
        "CHURNOPS_BQ_DATASET": "churnops",
        "CHURNOPS_USE_GCS": "1",
        "CHURNOPS_USE_BQ": "1",
        "CHURNOPS_USE_PUBSUB": "1",
    })

    from bootstrap_seed import bootstrap
    from common.warehouse import Warehouse
    from generator import run_generator
    from ingest import process_batch
    from reference import ReferenceWindow

    warehouse = Warehouse.default()
    if ReferenceWindow.load(warehouse) is None:
        print("No GCP reference window found; seeding a small labelled baseline in GCS and BigQuery.")
        # A compact multi-day baseline is enough to demonstrate drift and keeps
        # first-run BigQuery bootstrap latency reasonable for a live demo.
        bootstrap(seed_rows=3000, history_days=2, rows_per_day=400)

    trace_id = f"tr-gcp-demo-{datetime.datetime.now(datetime.timezone.utc):%Y%m%d%H%M%S}"
    batch = run_generator(
        scenario=args.scenario,
        severity=args.severity,
        batch_size=args.batch_size,
        trace_id=trace_id,
    )
    ingested = process_batch(batch_file_path=batch["parquet_path"], raise_on_quarantine=True)
    if ingested["status"] != "success":
        raise RuntimeError(f"GCP ingest did not succeed: {ingested['status']}")

    result = {
        "status": "submitted_to_gcp_event_chain",
        "project_id": args.project_id,
        "trace_id": trace_id,
        "batch_id": batch["batch_id"],
        "gcs_object": batch["gcs_uri"],
        "bigquery_tables": ["raw_events", "features_daily"],
        "pubsub_topic": "data-batch-landed",
        "cloud_run_services_to_watch": ["churnops-drift", "churnops-analyst", "churnops-trigger"],
        "next_stage": "Cloud Run drift detection publishes drift.detected; Pub/Sub invokes the analyst and Vertex pipeline trigger.",
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()
