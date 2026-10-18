# Runbook — a batch was quarantined

**Owner:** Person 1 (Data & Drift) · **Severity:** P2 by default, P1 if three consecutive batches fail
**Alert:** log-based metric `data_quality_quarantine` (structured log field `alert`, service `ingest`)

A quarantined batch means the ingest job refused data that broke
`contracts/features.yaml`. Nothing reached `raw_events` or `features_daily`, no
`data.batch.landed` was published, so **no drift verdict and no retraining will run
for that day**. The platform is safe, but it is blind until the batch is fixed or
explicitly skipped.

---

## 1. Confirm what happened (2 minutes)

```bash
# Cloud Logging: everything about this incident, one trace
gcloud logging read \
  'jsonPayload.service="ingest" AND jsonPayload.alert="data_quality_quarantine"' \
  --limit 5 --format json

# The structured error sits next to the batch in the quarantine bucket
gsutil cat gs://${PROJECT_ID}-quarantine/date=YYYY-MM-DD/error_<BATCH_ID>.json
```

Locally the same artefacts are under `data/quarantine/date=YYYY-MM-DD/`.

The error file lists every reason, each with `rule`, `column`, `observed` and
`threshold`:

| `rule` | What it means | Usual cause |
| --- | --- | --- |
| `pandera_check` | A row-level check failed (range, category set, id pattern) | Upstream shipped a new category or an impossible value |
| `max_null_fraction` | More than 2% of a feature is null | Broken join or dropped column upstream |
| `required_non_null` | A key or partition column is null | Generator/ETL bug |
| `max_duplicate_key_fraction` | `customer_id` repeats inside one batch | Batch written twice, or a bad fan-out join |
| `min_rows` | Fewer than 50 rows | Partial upload, or the job ran before the source was ready |
| `missing_column` | A contract feature is absent | Schema change upstream |

## 2. Decide which of the four cases it is

### Case A — genuinely bad data (most common)
Upstream regression: nulls, negative tenure, impossible charges.

1. Tell the producing team, quoting `batch_id` and the error file URI.
2. Leave the batch in quarantine (60-day retention — it is the evidence).
3. Once upstream re-publishes, re-land and re-ingest:
   ```bash
   gsutil cp gs://${PROJECT_ID}-quarantine/date=YYYY-MM-DD/batch_<ID>.parquet \
             gs://${PROJECT_ID}-landing/batches/date=YYYY-MM-DD/
   # Eventarc re-triggers ingest; the load is idempotent (MERGE on batch_id).
   ```

### Case B — legitimate new category (`new_plan`)
Marketing launched a plan the contract does not know, e.g. `Flexible-Monthly` in
`contract`. This is a **contract change, not a data fix**:

1. Open a PR adding the value to `contracts/features.yaml` and bump `version`.
2. Get approval from the consumer (P2 — the serving Pydantic models and the model's
   one-hot encoder both read this contract).
3. After merge, replay the batch as in Case A. The drift detector will still flag the
   category as unseen against the reference window, which is correct: the champion has
   never been trained on it, so a retrain is the right outcome.

### Case C — the threshold is wrong, the data is fine
If a 3% null rate is normal for a feature, change the rule rather than the data:
edit `validation.max_null_fraction` in `contracts/features.yaml` (or add a
feature-level exception), explain why in the PR, and replay.

### Case D — a one-off we accept losing
Short batch from a partial upload, superseded by the next day's batch. Record the
decision in the incident channel and move on; the gap is visible as a missing
partition on the Data dashboard.

## 3. Verify recovery

```bash
make inject-drift SCENARIO=none              # or wait for the next scheduled batch
python data/ingest/ingest.py <batch.parquet> # exits 0 and prints status=success
```

Check that:
- `raw_events` has the partition and `features_daily` the same row count,
- `data.batch.landed` appeared on the topic (or in `data/events_outbox/` locally),
- the drift service produced a report for the batch,
- the Data dashboard's quarantine counter has stopped rising.

## 4. If quarantine keeps firing

Three consecutive quarantined batches means the platform is effectively blind:

1. Escalate to P1 **and** the upstream owner; page if a demo or review is near.
2. Do **not** loosen the contract to get data flowing — a silently degraded model is
   worse than a visible gap.
3. If retraining is urgent, run the pipeline manually on the last good snapshot:
   `trigger_reason=manual`, naming the last clean `batch_id`.

## 5. Prevention

- Scenario tests (`tests/unit/test_generator_scenarios.py`) keep `corrupt_batch` and
  `new_plan` reproducing both failure modes on every CI run.
- The generator refuses to clip values when a scenario declares
  `clip_to_contract: false`, so this path is exercised deliberately, not by accident.
- Every contract change needs the producer **and** the consumer on the PR, plus a
  `version` bump (PRD §10.3).
