# ChurnOps — Automated Churn Pipeline with Drift Alerts

![GCP](https://img.shields.io/badge/Cloud-GCP-4285F4?style=flat&logo=googlecloud)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-009688?style=flat&logo=fastapi)
![Terraform](https://img.shields.io/badge/IaC-Terraform-844FBA?style=flat&logo=terraform)
![License](https://img.shields.io/badge/License-MIT-green)

ChurnOps is a college mini-project that demonstrates an end-to-end MLOps workflow for customer churn prediction. It generates subscriber data, detects drift, trains and evaluates a challenger model, and explains drift incidents. A local simulation is available for a repeatable live demo; selected platform components can also be deployed to Google Cloud.

The local demo uses real project code for data generation, validation, drift analysis, model training and quality gating. Its traffic rollout is a **simulation**; it does not change live serving traffic. The cloud trigger submits a Vertex AI Pipeline, while production-grade model serving, automated canary rollout and rollback are not part of this mini-project's verified demo path.

---

## 🏗️ Architecture Sketch & 3-Role Ownership

This diagram summarizes the project components. The local walkthrough is the
verified college demo path; it simulates the traffic rollout. The GCP trigger
and Vertex training pipeline can be deployed separately, while the full live
canary and rollback flow is outside the verified demo scope.

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
|   Vertex AI Pipeline: labelled BigQuery history -> train -> holdout evaluation   |
|   Quality Gate: ROC-AUC and Brier thresholds -> publish trained/rejected event   |
|   FastAPI Prediction Service (containerized inference component)                  |
|   Drift Analyst: Gemini when configured; deterministic fallback for local demo   |
+-----------------------------------------------------------------------------------+
                                         │  (model.trained)
                                         ▼
+-----------------------------------------------------------------------------------+
| PERSON 3: PROGRESSIVE DELIVERY                                                    |
|   Demo scope: local rollout narration; no live traffic shift or auto-rollback     |
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

## 🚀 Quickstart & College Demo

The local demo runs without GCP credentials. It writes data, reports and events
to local folders. The first run seeds a small local dataset; later runs reuse
the local state. To reset the demo, run `make clean` and then `make seed`.

```bash
# 1. Install dependencies
make setup

# 2. Bootstrap the data plane: seed data, history and a reference window
make seed

# 3. Verify the contracts and unit tests (optional before the demo)
make test-contracts
make test-unit

# 4. Run the end-to-end local incident demo
make simulate SCENARIO=price_hike SEVERITY=0.8
```

On Windows PowerShell, create an environment and install the component
dependencies before running the demo:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r data/generator/requirements.txt -r data/ingest/requirements.txt -r data/drift/requirements.txt -r ml/pipeline/requirements.txt -r ml/serving/requirements.txt -r llm/analyst/requirements.txt
py -m pip install pytest jsonschema ruff mypy matplotlib
py scripts/bootstrap_seed.py
py scripts/simulate_walking_skeleton.py --scenario price_hike --severity 0.8
```

### GCP-backed demo

For the cloud presentation, deploy the current source with Cloud Build, log in
with Application Default Credentials, then run `scripts/demo_gcp.py`. This path
stores the baseline and incoming batch in GCS/BigQuery and publishes real
Pub/Sub events. Pub/Sub invokes the Cloud Run drift detector; a critical event
fans out to the Cloud Run Vertex Gemini analyst and the existing Vertex AI
pipeline trigger. The Vertex pipeline trains from labelled BigQuery rows and
publishes its quality-gate result. The serving endpoint can be demonstrated
separately; automated live canary traffic promotion is not part of this demo.

```powershell
# From the repository root; rebuild and publish all six containers.
gcloud builds submit --project=churnops-512010 --region=us-central1 `
  --config=cloudbuild/build-images.yaml `
  --substitutions=_TAG=gcpdemo .

# Deploy the published image tag with the authenticated local Terraform CLI.
Set-Location infra/envs/dev
terraform init -backend-config="bucket=churnops-512010-tfstate" -backend-config="prefix=terraform/state"
terraform apply -var="project_id=churnops-512010" -var="image_tag=gcpdemo"
Set-Location ../..

# Authenticate the local demo driver to the same project.
gcloud config set project churnops-512010
gcloud auth application-default login
.\.venv\Scripts\Activate.ps1
python scripts/demo_gcp.py --scenario price_hike --severity 0.8
```

The cloud driver seeds a small baseline in GCS and BigQuery if no shared
reference sample exists. It then emits one batch to the real Pub/Sub chain.
Cloud Run logs and the Vertex AI Pipelines page show the asynchronous stages.
This starts a Vertex training run and Vertex Gemini inference, which consume
project resources. The pipeline may be suppressed by the configured four-hour
drift cooldown if another critical incident has already run recently. The
training component is configured for 1 CPU and 4 GiB, but this GCP project has a
Vertex AI custom-training CPU quota of 1 in `us-central1`; Vertex still rejected
the training task against that limit. Request 4 CPUs under **Vertex AI API →
Custom model training CPUs for N1/E2 machine types → us-central1** to complete
the cloud retraining stage. Quota approval is controlled by Google Cloud.

### What `make simulate` does

| Phase | What actually runs |
|---|---|
| 0 | Reads the frozen reference window and the serving champion |
| 1 | **Generator job** samples a batch from the fitted profile and injects the scenario; churn labels are withheld for 14 simulated days |
| 2 | **Ingest code** validates the batch and writes to the local warehouse/outbox (no BigQuery or Pub/Sub in local mode) |
| 3 | **Drift detector** computes feature drift, writes local reports and emits a local `drift.detected` event when thresholds are exceeded |
| 4 | **Drift Analyst** produces a report; Vertex Gemini is used only when configured, otherwise the demo's fallback is used |
| 5 | **Training code** snapshots labelled history, trains a challenger and applies the quality gate |
| 6 | **Rollout narration** displays illustrative traffic stages only when the gate passes; it does not shift real traffic. A rejected challenger leaves the champion/reference unchanged |
| 7 | **Drift service** refreshes the reference window after promotion; it stays on the current champion when the challenger is rejected |
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
