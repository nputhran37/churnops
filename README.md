# ChurnOps — Automated Churn Pipeline with Drift Alerts

![GCP](https://img.shields.io/badge/Cloud-GCP-4285F4?style=flat&logo=googlecloud)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-009688?style=flat&logo=fastapi)
![Terraform](https://img.shields.io/badge/IaC-Terraform-844FBA?style=flat&logo=terraform)
![License](https://img.shields.io/badge/License-MIT-green)

ChurnOps is a production-grade, event-driven MLOps and LLMOps platform on Google Cloud Platform that keeps a churn prediction model trustworthy with **zero human intervention in the loop**.

It observes incoming subscriber data, detects statistical drift before labels arrive, continuously retrains, executes quality gates, deploys canary traffic splits with automated rollback, and uses **Gemini on Vertex AI** as an automated "Drift Analyst" to narrate every incident in plain, executive-ready English.

---

## 🏗️ Architecture & 3-Role Ownership

The platform connects three decoupled planes through Pub/Sub events, BigQuery tables, Cloud Storage, and Vertex AI Model Registry:

```
+-----------------------------------------------------------------------------------+
| PERSON 1: DATA & DRIFT PLANE                                                      |
|   Cloud Run Job: Generator (profile-fitted sampling + config-driven drift)        |
|       -> Parquet batch in GCS landing, churn labels withheld for 14 days          |
|   Cloud Run Job: Ingest (contract-generated Pandera + batch quality rules)        |
|       -> quarantine on failure, else MERGE raw_events + features_daily, then      |
|          publishes 'data.batch.landed'                                            |
|   Cloud Run: Drift Detector (PSI/KS, Jensen-Shannon/chi2, prediction drift)       |
|       -> report.json + report.html, drift_reports row, cooldown-gated             |
|          'drift.detected'; refreshes reference_window on 'model.promoted'         |
+-----------------------------------------------------------------------------------+
                                         │  (drift.detected)
                                         ▼
+-----------------------------------------------------------------------------------+
| PERSON 3: PLATFORM & DEVOPS PLANE                                                 |
|   Cloud Run Function: Trigger Function (concurrency lock + trace_id idempotency)  |
|   Launches Vertex AI Pipelines                                                    |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
| PERSON 2: ML & LLMOps PLANE                                                       |
|   Vertex AI Pipeline: Snapshot -> Validate -> Split -> Train -> Holdout Evaluate  |
|   Quality Gate: ROC-AUC >= Champion - 0.005 & Recall@70P -> Register Challenger   |
|   Cloud Run: FastAPI Prediction Service (p95 latency < 300ms SLO)                |
|   Cloud Run: Gemini Drift Analyst (Structured JSON + Numeric Guardrails + Slack)  |
+-----------------------------------------------------------------------------------+
                                         │  (model.trained)
                                         ▼
+-----------------------------------------------------------------------------------+
| PERSON 3: PROGRESSIVE DELIVERY                                                    |
|   Cloud Build: Canary traffic split 10% -> 50% -> 100% on Cloud Run               |
|   Auto-Rollback on SLO breach; Reassigns Vertex Registry alias 'champion'         |
+-----------------------------------------------------------------------------------+
```

---

## 📂 Monorepo Layout

```
churnops/
├── contracts/               # Machine-readable contracts (Features, BQ, Events, API)
│   ├── bq/                  # BigQuery table schemas (raw_events, features, logs)
│   ├── events/              # Pub/Sub JSON schemas and sample payloads
│   ├── api/                 # OpenAPI 3.0 specification for Prediction Service
│   ├── features.yaml        # Shared feature dictionary & transform spec
│   ├── storage.md           # GCS bucket hierarchy and lifecycle rules
│   ├── registry.md          # Vertex Model Registry aliases & lineage labels
│   └── logging.md           # Structured JSON logging schema
├── infra/                   # Terraform Infrastructure as Code
│   ├── modules/             # apis, storage, bigquery, pubsub, cloud_run, secrets
│   └── envs/dev/            # Dev environment composition
├── data/                    # Person 1: Data Plane
│   ├── common/              # Shared library: contracts, features, events, storage, warehouse
│   ├── generator/           # Profile-fitted generator + scenarios.yaml drift catalogue
│   ├── ingest/              # Contract-generated Pandera validation + warehouse loading
│   └── drift/               # PSI/KS/JS/chi2 detector, reference window, realised performance
├── ml/                      # Person 2: ML Plane
│   ├── pipeline/            # Training script, KFP v2 pipeline, gate policy
│   └── serving/             # FastAPI scale-to-zero inference microservice
├── llm/                     # Person 2: LLMOps Plane
│   ├── analyst/             # Gemini Drift Analyst with numeric guardrails
│   ├── prompts/             # Versioned prompt YAMLs (v1.yaml)
│   └── evals/               # Golden scenario dataset and CI eval runner
├── ops/                     # Person 3: Platform Plane
│   ├── trigger_fn/          # Eventarc / Pub/Sub continuous training launcher
│   └── dashboards/          # Cloud Monitoring dashboards as code
├── cloudbuild/              # Native GCP CI/CD/CT pipelines
│   ├── ci.yaml              # Lint, unit tests, contracts, terraform plan, evals
│   ├── cd.yaml              # Container builds, terraform apply, KFP upload
│   └── release-model.yaml   # Progressive canary deployment and rollback
├── tests/                   # Integration and contract validation tests
└── scripts/                 # Bootstrap and demo simulation runners
```

---

## 🚀 Quickstart & Demo Simulation

The whole loop runs locally in under a minute with **zero GCP credentials** — the
data plane writes to local directories and a local event outbox instead of GCS,
BigQuery and Pub/Sub, selected by environment variables, not by different code.

```bash
# 1. Install dependencies
make setup

# 2. Bootstrap the data plane: fit the generator profile, load the seed and 30 days
#    of history, freeze the reference window, materialise the training snapshot
make seed

# 3. Verify the contracts and the data-plane unit tests
make test-contracts
make test-unit

# 4. Run the full automated incident demo
make simulate SCENARIO=price_hike SEVERITY=0.8
```

### What `make simulate` does

| Phase | What actually runs |
|---|---|
| 0 | Reads the frozen reference window and the serving champion |
| 1 | **Generator job** samples a batch from the fitted profile and injects the scenario; churn labels are withheld for 14 simulated days |
| 2 | **Ingest job** validates with the contract-generated Pandera schema, MERGEs `raw_events` + `features_daily`, publishes `data.batch.landed` |
| 3 | **Drift service** computes PSI/KS per numeric feature, Jensen-Shannon/chi-square per categorical, and prediction drift against the champion's score distribution; writes `report.json`, `report.html` and a `drift_reports` row; publishes `drift.detected` unless the cooldown suppresses it |
| 4 | **Drift Analyst** (Gemini) narrates the incident |
| 5 | **Training pipeline** snapshots the labelled history, trains and gates the challenger |
| 6 | **Progressive delivery** shifts traffic 10% → 50% → 100% |
| 7 | **Drift service** refreshes the reference window to the new champion |
| 8 | Two simulated weeks later the labels arrive and the realised champion AUC is backfilled onto the drift report — the proof the alert predicted degradation |

---

## 🛰️ Data plane (Person 1)

Everything the data plane does is driven by a contract, never by constants in code.

| Concern | Single source of truth |
|---|---|
| Features, dtypes, ranges, categories, transforms | `contracts/features.yaml` |
| Batch quality rules (volume, null rate, duplicate keys) | `contracts/features.yaml` → `validation:` |
| Fitted scaling constants (mean/std/median/mode) | `contracts/feature_stats.json` (written by `make seed`) |
| Drift scenarios and severity knob | `data/generator/scenarios.yaml` |
| Drift thresholds, priority features, cooldown | `data/drift/drift_policy.yaml` |
| Warehouse schemas | `contracts/bq/*.json` |
| Event payloads (validated before publish) | `contracts/events/*.json` |

```bash
make eda                                   # docs/eda/ plots + written summary
make inject-drift SCENARIO=new_plan SEVERITY=1.0
make ingest BATCH="data/landing/batches/date=2026-10-17/batch_<id>.parquet"
make detect-drift BATCH="data/landing/batches/date=2026-10-17/batch_<id>.parquet"
make refresh-reference MODEL=v4            # what model.promoted triggers in production
make realised-performance                  # backfill realised AUC once labels arrive
```

**Drift scenarios** (`data/generator/scenarios.yaml`, severity 0–1 scales every shift):

| Scenario | Story | Expected outcome |
|---|---|---|
| `none` | Stationary traffic | Silence |
| `price_hike` | List-price increase across all plans | `monthly_charges` PSI breaches 0.20 → `drift.detected` |
| `competitor_launch` | Rival undercuts consumer plans | `tenure` collapses, charges fall |
| `seasonal_spike` | Festive streaming bundle | Several features drift together |
| `new_plan` | Unseen `Flexible-Monthly` contract | Batch quarantined until the contract is bumped |
| `corrupt_batch` | Upstream ETL regression | Batch quarantined with a structured error |

**Delayed labels.** A batch lands with `churn` NULL; the generator releases its
labels 14 simulated days later into the `labels` table. That is why drift has to be
the early-warning signal, and it is what makes
`drift_reports.realised_auc` meaningful.

**Running against GCP.** The same code paths switch to cloud services with
`CHURNOPS_USE_GCS=1`, `CHURNOPS_USE_BQ=1`, `CHURNOPS_USE_PUBSUB=1` plus
`CHURNOPS_PROJECT_ID` / `CHURNOPS_BQ_DATASET`. Images build from the repo root so
the shared library ships with each service:
`docker build -f data/ingest/Dockerfile -t churnops-ingest .`

**Runbook:** `docs/runbooks/batch_quarantined.md` —
what to do when a batch is quarantined.
**Dashboards:** `ops/dashboards/data_quality_dashboard.json` and
`ops/dashboards/data_quality_metrics.md` list the log-based metrics P3 needs to declare.

---

## 📋 Verification & Testing

| Test Command | Purpose |
|---|---|
| `make test-contracts` | Validates `features.yaml`, the BigQuery schemas, the event JSON schemas and OpenAPI 3.0 |
| `make test-unit` | 62 data-plane tests: golden drift scenarios, validation and quarantine, drift policy and cooldown, contract/SQL parity, event payloads |
| `make test-llm` | 30-case golden benchmark scoring prompt faithfulness, coverage and actionability |
| `make test-e2e` | End-to-end walking skeleton across all three roles in a temporary workspace |
| `make terraform-plan` | Dry-run Terraform plan against the dev environment |

### Platform monitoring setup (Person 3)

Terraform creates the Cloud Monitoring dashboards and baseline alerts from `ops/dashboards/`. To receive alert notifications, create a notification channel in Cloud Monitoring and pass its full resource name through `notification_channels` in `infra/envs/dev/terraform.tfvars` (for example, `projects/<project-id>/notificationChannels/<channel-id>`). The example defaults to an empty list, so incidents remain visible in Monitoring but are not sent to an email or messaging destination until a channel is configured.

### Terraform state bootstrap

The first deployment creates a versioned GCS bucket for remote Terraform state. Run `make terraform-bootstrap` once from a shell with `gcloud` authenticated and permission to create buckets in the project. Cloud Build performs the same check on deploy. Then `make terraform-plan` uses the remote state backend. The state bucket is `gs://<project-id>-tfstate` and is intentionally separate from the application data buckets.
