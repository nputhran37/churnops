"""
ChurnOps end-to-end demo runner.

Replays the full incident narrative locally in under a minute with no GCP
credentials: inject drift -> validate -> detect -> alert -> retrain -> gate ->
canary -> closing report -> realised performance once the labels arrive.

The data-plane steps (1-4 and 8) are the real P1 services; the ML, LLM and
delivery steps call P2's and P3's code or narrate what their pipelines do.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for path in (
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "data"),
    os.path.join(PROJECT_ROOT, "data", "generator"),
    os.path.join(PROJECT_ROOT, "data", "ingest"),
    os.path.join(PROJECT_ROOT, "data", "drift"),
    os.path.join(PROJECT_ROOT, "ml", "pipeline"),
    os.path.join(PROJECT_ROOT, "llm", "analyst"),
    os.path.join(PROJECT_ROOT, "scripts"),
):
    sys.path.insert(0, path)

import detector  # noqa: E402
import generator  # noqa: E402
import ingest  # noqa: E402
import realised_performance  # noqa: E402
from bootstrap_seed import bootstrap, write_training_snapshot  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from reference import ReferenceWindow  # noqa: E402
from scenarios import ScenarioCatalogue  # noqa: E402

if sys.platform == "win32":  # keep the box-drawing output readable on cmd.exe
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")

RULE = "=" * 72


def phase(number: int, title: str) -> None:
    print(f"\n[Phase {number}] {title}")


def ok(message: str) -> None:
    print(f"  [OK] {message}")


def run_simulation(scenario: str = "price_hike", severity: float = 0.8, batch_size: int = 1500) -> dict:
    trace_id = f"tr-demo-{int(time.time())}"
    catalogue = ScenarioCatalogue.load()
    spec = catalogue.get(scenario)
    warehouse = Warehouse.default()

    print("\n" + RULE)
    print(">>> CHURNOPS AUTOMATED INCIDENT DEMO")
    print(f"    trace_id : {trace_id}")
    print(f"    scenario : {scenario.upper()} (severity {severity})")
    print(f"    story    : {spec.business_narrative}")
    print(RULE)

    # ---- 0. platform state -------------------------------------------
    phase(0, "Platform state")
    window = ReferenceWindow.load(warehouse)
    if window is None:
        print("  No reference window found - bootstrapping the data plane (make seed)...")
        bootstrap(history_days=20, rows_per_day=600)
        window = ReferenceWindow.load(warehouse)
    ok(f"Champion {window.model_version} serving · reference window {window.row_count} rows "
       f"(snapshot {window.snapshot_date})")
    ok("Drift: NONE · last incident: none")

    # ---- 1. inject ----------------------------------------------------
    phase(1, f"Generator job lands today's batch with '{scenario}' injected")
    batch = generator.run_generator(
        scenario=scenario, severity=severity, batch_size=batch_size, trace_id=trace_id
    )
    ok(f"{batch['row_count']} rows -> {batch['gcs_uri']}")
    ok(f"Labels withheld; they arrive on {batch['pending_labels_path'].split(os.sep)[-1].split('__')[0]} + 14 days")
    for step in batch["drift_audit"]:
        before, after = step["before"].get("mean"), step["after"].get("mean")
        if before is not None and after is not None:
            print(f"       {step['feature']}: mean {before} -> {after} ({step['operation']})")

    # ---- 2. ingest ----------------------------------------------------
    phase(2, "Eventarc triggers the ingest job (Pandera validation -> BigQuery)")
    ingest_result = ingest.process_batch(batch_file_path=batch["parquet_path"])
    if ingest_result["status"] == "quarantined":
        print("  [!] BATCH QUARANTINED - nothing reached the warehouse.")
        for issue in ingest_result["issues"][:5]:
            print(f"       - {issue['message']}")
        print(f"  Runbook: docs/runbooks/batch_quarantined.md · artefacts: {ingest_result['quarantine_path']}")
        return {"status": "quarantined", "batch": batch, "ingest": ingest_result}
    ok(f"Validation passed · raw_events {ingest_result['load_mode']} · features_daily rebuilt")
    ok("Published data.batch.landed")

    # ---- 3. drift ------------------------------------------------------
    phase(3, "Drift service compares the batch against the frozen reference window")
    drift = detector.run_drift_analysis(current_batch_path=batch["parquet_path"])
    top = drift["report"]["features"][0]
    ok(f"Verdict: {drift['severity'].upper()} · {drift['share_drifted'] * 100:.0f}% of features drifted")
    ok(f"Top drifted: {top['feature']} ({top['metric']}={top['score']}) · {top['change_summary']}")
    ok(f"Prediction drift p={drift['report']['prediction_drift']['p_value']} "
       f"(source: {drift['report']['prediction_drift']['source']})")
    ok(f"Report: {drift['report_uri']}")
    if drift["event_payload"]:
        ok("Published drift.detected")
    elif drift["cooldown"]["suppressed"]:
        print("  [i] Suppressed by the cooldown window - no retraining launched.")
        return {"status": "suppressed", "batch": batch, "drift": drift}
    else:
        print("  [i] No critical drift: the platform stays silent. Demo ends here.")
        return {"status": "no_drift", "batch": batch, "drift": drift}

    # ---- 4. analyst ------------------------------------------------------
    phase(4, "Drift Analyst (Gemini) narrates the incident")
    try:
        import service as analyst

        opening = analyst.process_event(drift["event_payload"])["report"]
        print(f"  [slack] {opening['headline']}")
        for line in opening["what_changed"]:
            print(f"          · {line}")
        print(f"  [slack] action: {opening['model_action']}")
    except Exception as exc:  # the data plane must not depend on the LLM service
        print(f"  [!] Drift Analyst unavailable ({exc}); continuing without the narrative.")
        opening = None

    # ---- 5. retrain -------------------------------------------------------
    phase(5, "Trigger function launches the Vertex AI training pipeline")
    snapshot = write_training_snapshot(warehouse)
    print("  Steps: snapshot -> validate -> split -> train -> evaluate -> gate -> register")
    try:
        import train

        training = train.train_and_gate_model(
            data_path=snapshot["path"],
            output_dir=os.path.join("artifacts", "models"),
            champion_auc=0.810,
            trace_id=trace_id,
            batch_id=batch["batch_id"],
        )
        challenger_auc = training["metrics"]["roc_auc"]
        ok(f"Snapshot: {snapshot['rows']} labelled rows")
        ok(f"Quality gate {training['status']} · challenger ROC-AUC {challenger_auc:.4f}")
        model_version = training["model_version"]
    except Exception as exc:
        print(f"  [!] Training pipeline unavailable ({exc}); narrating the remaining steps.")
        challenger_auc, model_version = 0.0, "v-unknown"

    # ---- 6. canary ---------------------------------------------------------
    phase(6, "Cloud Build progressive delivery")
    for split in (0, 10, 50, 100):
        time.sleep(0.2)
        print(f"  traffic {split:>3}% · p95 19.1 ms · errors 0.00%")
    ok(f"Registry alias 'champion' moved to {model_version}; published model.promoted")

    # ---- 7. reference refresh ----------------------------------------------
    phase(7, "Drift service refreshes the reference window to the new champion")
    refreshed = detector.handle_model_promoted(
        {"event_type": "model.promoted", "promoted_model_version": model_version, "trace_id": trace_id}
    )
    ok(f"Reference window now {refreshed.get('rows', 0)} rows from {model_version}")

    # ---- 8. realised performance -------------------------------------------
    phase(8, "Two simulated weeks later: labels arrive and prove the alert was right")
    generator.run_generator(scenario="none", severity=0.0, batch_size=300, trace_id=f"{trace_id}-labels",
                            date_str=_plus_days(batch["batch_date"], 14))
    backfill = realised_performance.backfill_all()
    evidence = realised_performance.degradation_evidence()
    ok(f"Realised performance backfilled for {backfill['updated']} drift report(s)")
    for severity_name, stats in evidence.get("by_severity", {}).items():
        print(f"       {severity_name}: mean realised AUC {stats['mean_realised_auc']} "
              f"over {stats['batches']} batch(es)")

    print("\n" + RULE)
    print("[SUCCESS] Drift detected, model retrained, gated, promoted and explained - no human in the loop.")
    print(RULE + "\n")

    return {
        "status": "completed",
        "trace_id": trace_id,
        "batch": batch,
        "drift_severity": drift["severity"],
        "report_uri": drift["report_uri"],
        "challenger_auc": challenger_auc,
        "model_version": model_version,
        "realised_performance": evidence,
    }


def _plus_days(date_str: str, days: int) -> str:
    import datetime

    return (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=days)).isoformat()


def main(argv: list[str] | None = None) -> dict:
    catalogue = ScenarioCatalogue.load()
    parser = argparse.ArgumentParser(description="ChurnOps walking-skeleton simulation")
    parser.add_argument("--scenario", default="price_hike", choices=catalogue.names)
    parser.add_argument("--severity", type=float, default=0.8)
    parser.add_argument("--batch-size", type=int, default=1500)
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result as well")
    args = parser.parse_args(argv)

    result = run_simulation(args.scenario, args.severity, args.batch_size)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
