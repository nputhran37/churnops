"""
End-to-end integration walkthrough for the ChurnOps walking skeleton.

Runs the real services of all three planes against an isolated temporary data
plane: bootstrap -> inject drift -> validate -> detect -> alert -> retrain ->
gate -> refresh reference -> realised performance.

`make test-e2e` runs this; Integration Friday runs it against dev.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
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
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("CHURNOPS_CONTRACTS_DIR", os.path.join(PROJECT_ROOT, "contracts"))


class TestChurnOpsWalkingSkeleton(unittest.TestCase):
    """One incident, one trace_id, every plane."""

    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="churnops-e2e-")
        for role in ("LANDING", "QUARANTINE", "REPORTS", "WAREHOUSE"):
            directory = os.path.join(cls.workdir, role.lower())
            os.makedirs(directory, exist_ok=True)
            os.environ[f"CHURNOPS_{role}_DIR"] = directory
        cls.outbox = os.path.join(cls.workdir, "events_outbox")
        os.makedirs(cls.outbox, exist_ok=True)
        cls.trace_id = "tr-walkthrough-e2e-001"

    @classmethod
    def tearDownClass(cls):
        for role in ("LANDING", "QUARANTINE", "REPORTS", "WAREHOUSE"):
            os.environ.pop(f"CHURNOPS_{role}_DIR", None)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_full_automated_loop(self):
        import detector
        import generator
        import ingest
        import realised_performance
        import service
        import train
        from bootstrap_seed import bootstrap, write_training_snapshot
        from common.events import read_outbox, validate_event
        from common.warehouse import Warehouse
        from reference import ReferenceWindow

        print("\n" + "=" * 70)
        print("CHURNOPS WALKING SKELETON - END TO END")
        print("=" * 70)

        # --- Step 1: bootstrap the data plane --------------------------
        print("\n[1] Bootstrapping seed, history and reference window...")
        boot = bootstrap(seed_rows=3000, history_days=10, rows_per_day=400, today="2026-10-01")
        warehouse = Warehouse.default()
        self.assertGreater(boot["training_snapshot"]["rows"], 1000)
        self.assertEqual(boot["reference_window"]["model_version"], "v-initial-champion")
        print(f"[OK] {boot['history_rows']} historical rows, reference window "
              f"{boot['reference_window']['rows']} rows")

        # --- Step 2: champion model ------------------------------------
        print("\n[2] Training the initial champion...")
        champion = train.train_and_gate_model(
            data_path=boot["training_snapshot"]["path"],
            output_dir=os.path.join(self.workdir, "models"),
            champion_auc=0.70,
            trace_id="tr-init-champ",
            batch_id="b-seed-000",
            events_dir=self.outbox,
        )
        self.assertEqual(champion["status"], "gate_passed")
        champion_auc = champion["metrics"]["roc_auc"]
        print(f"[OK] Champion ROC-AUC {champion_auc:.4f}")

        # --- Step 3: inject drift ---------------------------------------
        print("\n[3] Injecting price_hike at severity 0.8...")
        batch = generator.run_generator(
            scenario="price_hike",
            severity=0.8,
            date_str="2026-10-01",
            batch_size=1200,
            trace_id=self.trace_id,
        )
        self.assertTrue(os.path.exists(batch["parquet_path"]))
        self.assertEqual(batch["expected_drift"][0], "monthly_charges")

        # --- Step 4: ingest + validate -----------------------------------
        print("\n[4] Ingesting and validating the batch...")
        ingested = ingest.process_batch(batch_file_path=batch["parquet_path"], events_dir=self.outbox)
        self.assertEqual(ingested["status"], "success")
        landed = read_outbox("data.batch.landed", self.outbox)
        validate_event("data.batch.landed", landed[-1])
        print(f"[OK] {ingested['row_count']} rows loaded, data.batch.landed published")

        # --- Step 5: drift detection --------------------------------------
        print("\n[5] Detecting drift against the reference window...")
        drift = detector.run_drift_analysis(current_batch_path=batch["parquet_path"], events_dir=self.outbox)
        self.assertEqual(drift["severity"], "critical")
        self.assertIsNotNone(drift["event_payload"])
        validate_event("drift.detected", drift["event_payload"])
        drifted_features = [f["feature"] for f in drift["event_payload"]["drifted_features"]]
        self.assertEqual(drifted_features[0], "monthly_charges")
        self.assertEqual(drift["event_payload"]["trace_id"], self.trace_id)
        print(f"[OK] CRITICAL drift, top feature {drifted_features[0]}")

        # --- Step 6: continuous training -----------------------------------
        print("\n[6] Retraining on the refreshed snapshot...")
        snapshot = write_training_snapshot(warehouse)
        challenger = train.train_and_gate_model(
            data_path=snapshot["path"],
            output_dir=os.path.join(self.workdir, "models"),
            champion_auc=champion_auc,
            trace_id=self.trace_id,
            batch_id=batch["batch_id"],
            events_dir=self.outbox,
        )
        self.assertIn(challenger["status"], ("gate_passed", "gate_failed"))
        print(f"[OK] Gate {challenger['status']} with ROC-AUC {challenger['metrics']['roc_auc']:.4f}")

        # --- Step 7: LLM incident report ------------------------------------
        print("\n[7] Drift Analyst narrates the incident...")
        analyst = service.process_event(drift["event_payload"])
        self.assertEqual(analyst["status"], "success")
        self.assertTrue(analyst["guardrail_passed"])
        print(f"[OK] {analyst['report']['headline']}")

        # --- Step 8: promotion refreshes the reference window -----------------
        print("\n[8] Promoting the challenger and refreshing the reference window...")
        refreshed = detector.handle_model_promoted(
            {
                "event_type": "model.promoted",
                "promoted_model_version": challenger["model_version"],
                "trace_id": self.trace_id,
            }
        )
        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(ReferenceWindow.load(warehouse).model_version, challenger["model_version"])
        print(f"[OK] Reference window now tracks {challenger['model_version']}")

        # --- Step 9: delayed labels prove the alert was right -------------------
        print("\n[9] Releasing delayed labels and backfilling realised performance...")
        generator.run_generator(scenario="none", severity=0.0, date_str="2026-10-15", batch_size=300)
        backfill = realised_performance.backfill_all()
        self.assertGreaterEqual(backfill["updated"], 1)
        report_row = next(
            row for row in warehouse.read_rows("drift_reports") if row["batch_id"] == batch["batch_id"]
        )
        self.assertIsNotNone(report_row["realised_auc"])
        print(f"[OK] Realised champion AUC on the drifted batch: {report_row['realised_auc']}")

        print("\n" + "=" * 70)
        print("WALKING SKELETON COMPLETED END TO END")
        print("=" * 70)


if __name__ == "__main__":
    unittest.main(verbosity=2)
