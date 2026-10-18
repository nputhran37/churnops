# Log-based metrics the data plane emits (P1 → P3)

`ops/dashboards/data_quality_dashboard.json` plots these. They all come from the
structured logs defined in `contracts/logging.md`, so P3 only needs to declare the
log-based metrics in Terraform — the services already emit the fields.

| Metric id | Type | Log filter | Value / labels |
| --- | --- | --- | --- |
| `churnops/rows_ingested` | distribution | `jsonPayload.service="ingest" AND jsonPayload.metrics.rows_loaded>0` | value `jsonPayload.metrics.rows_loaded`, label `batch_id` |
| `churnops/batches_quarantined` | counter | `jsonPayload.service="ingest" AND jsonPayload.alert="data_quality_quarantine"` | label `rule` from `jsonPayload.metrics.issues[0].rule` |
| `churnops/feature_null_fraction` | distribution | `jsonPayload.service="ingest" AND jsonPayload.metrics.null_fraction>0` | value `jsonPayload.metrics.null_fraction`, label `feature` |
| `churnops/feature_psi_score` | distribution | `jsonPayload.service="drift-detector" AND jsonPayload.message=~"^Feature drift"` | value `jsonPayload.metrics.score`, labels `feature`, `metric`, `feature_severity` |
| `churnops/drift_verdicts` | counter | `jsonPayload.service="drift-detector" AND jsonPayload.message=~"^Drift verdict"` | label `verdict_severity` |
| `churnops/drift_detected` | counter | `jsonPayload.service="drift-detector" AND jsonPayload.alert="drift_detected"` | label `batch_id` |
| `churnops/ingest_latency_ms` | distribution | `jsonPayload.service="ingest" AND jsonPayload.latency_ms>0` | value `jsonPayload.latency_ms` |

## Suggested alert policies

| Policy | Condition | Why |
| --- | --- | --- |
| Batch quarantined | `churnops/batches_quarantined` > 0 in 24h | The platform is blind for that day — see `docs/runbooks/batch_quarantined.md` |
| No batch landed | `churnops/rows_ingested` absent for 26h | The scheduler or the generator job is down |
| Drift storm | `churnops/drift_detected` > 2 in 24h | The cooldown is mis-tuned or the reference window is stale |
| Null-rate creep | `churnops/feature_null_fraction` > 0.01 for 3 consecutive batches | Upstream degrading before it breaks the contract |

## BigQuery panels (Looker Studio)

```sql
-- Drift timeline with incidents, for the demo page
SELECT batch_date, severity, share_of_drifted_features, realised_auc,
       JSON_VALUE(top_drifted_features, '$[0].feature') AS top_feature
FROM `${PROJECT_ID}.churnops.drift_reports`
ORDER BY batch_date;

-- Does a drift alert actually predict degradation?
SELECT severity, COUNT(*) AS batches, ROUND(AVG(realised_auc), 4) AS mean_realised_auc
FROM `${PROJECT_ID}.churnops.drift_reports`
WHERE realised_auc IS NOT NULL
GROUP BY severity;

-- Label latency: how long until a batch becomes trainable
SELECT batch_date, MIN(label_available_at) AS labels_ready,
       DATE_DIFF(MIN(label_available_at), batch_date, DAY) AS delay_days
FROM `${PROJECT_ID}.churnops.labels`
GROUP BY batch_date ORDER BY batch_date DESC LIMIT 30;
```
