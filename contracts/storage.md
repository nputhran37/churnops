# ChurnOps Cloud Storage Layout & Lifecycle Policy

All Cloud Storage buckets adhere to standardized prefix conventions and lifecycle policies to control GCP storage costs.

## Bucket Inventory

| Bucket Identifier | Purpose | Retention / Lifecycle | Owner |
|---|---|---|---|
| `gs://${PROJECT_ID}-landing/` | Daily incoming batches & seed data | 30 days auto-delete; coldline transition at 7 days | P1 |
| `gs://${PROJECT_ID}-quarantine/` | Batches failing Pandera data validation | 60 days retention for debugging | P1 |
| `gs://${PROJECT_ID}-artifacts/` | Compiled KFP pipelines, model joblib & metadata | Versioned; 90 days retention | P2 |
| `gs://${PROJECT_ID}-reports/` | Evidently HTML drift reports & LLM incident logs | 30 days retention | P1 / P2 |

## Path Hierarchy

```
gs://${PROJECT_ID}-landing/
  ├── seed/
  │   └── telco_churn_seed.parquet
  └── batches/
      └── date=YYYY-MM-DD/
          └── batch_${BATCH_ID}.parquet

gs://${PROJECT_ID}-quarantine/
  └── date=YYYY-MM-DD/
      ├── batch_${BATCH_ID}.parquet
      └── error_log.json

gs://${PROJECT_ID}-artifacts/
  ├── pipelines/
  │   └── churn_retraining_${GIT_SHA}.yaml
  └── models/
      └── ${MODEL_VERSION}/
          ├── model.joblib
          ├── preprocessor.joblib
          └── metadata.json

gs://${PROJECT_ID}-reports/
  └── drift/
      └── ${BATCH_ID}/
          ├── report.html
          └── report.json
```
