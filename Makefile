.PHONY: help setup seed eda inject-drift ingest detect-drift realised-performance refresh-reference simulate \
        test test-unit test-contracts test-e2e test-llm lint terraform-bootstrap terraform-init terraform-plan clean

PYTHON ?= python
PROJECT_ID ?= churnops-512010
REGION ?= us-central1
SCENARIO ?= price_hike
SEVERITY ?= 0.8
DATE ?=
BATCH_SIZE ?= 1500
BATCH ?=

help:
	@echo "======================================================================="
	@echo "ChurnOps - Automated Churn Pipeline with Drift Alerts"
	@echo "======================================================================="
	@echo "Data plane (Person 1)"
	@echo "  make seed                 - Fit the profile, load the seed + history, freeze reference window"
	@echo "  make eda                  - Regenerate docs/eda/ plots and the written summary"
	@echo "  make inject-drift         - Generate a batch (SCENARIO, SEVERITY, DATE, BATCH_SIZE)"
	@echo "  make ingest BATCH=<path>  - Validate and load one landed batch"
	@echo "  make detect-drift BATCH=<path> - Score drift and emit drift.detected when critical"
	@echo "  make refresh-reference MODEL=<version> - Rebuild the reference window after a promotion"
	@echo "  make realised-performance - Backfill realised champion AUC from delayed labels"
	@echo ""
	@echo "Platform"
	@echo "  make setup                - Install development dependencies"
	@echo "  make simulate             - Run the full automated incident demo"
	@echo "  make test                 - Contracts + unit + LLM evals + end-to-end"
	@echo "  make test-unit            - Data-plane unit and golden scenario tests"
	@echo "  make test-contracts       - Validate schemas, events and OpenAPI"
	@echo "  make test-llm             - LLMOps prompt evaluation suite"
	@echo "  make test-e2e             - End-to-end walking skeleton"
	@echo "  make lint                 - ruff + mypy"
	@echo "  make terraform-bootstrap  - Create versioned remote Terraform state bucket (one-time)"
	@echo "  make terraform-init/plan  - Terraform dev environment"
	@echo "  make clean                - Remove local batches, warehouse, reports and caches"
	@echo "======================================================================="

setup:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r data/generator/requirements.txt
	$(PYTHON) -m pip install -r data/ingest/requirements.txt
	$(PYTHON) -m pip install -r data/drift/requirements.txt
	$(PYTHON) -m pip install -r ml/pipeline/requirements.txt
	$(PYTHON) -m pip install -r ml/serving/requirements.txt
	$(PYTHON) -m pip install -r llm/analyst/requirements.txt
	$(PYTHON) -m pip install pytest jsonschema ruff mypy matplotlib

# ---------------------------------------------------------------- data plane
seed:
	$(PYTHON) scripts/bootstrap_seed.py

eda:
	$(PYTHON) scripts/eda.py

inject-drift:
	$(PYTHON) data/generator/generator.py --scenario $(SCENARIO) --severity $(SEVERITY) \
		--batch-size $(BATCH_SIZE) $(if $(DATE),--date $(DATE),)

ingest:
	@test -n "$(BATCH)" || (echo "Usage: make ingest BATCH=data/landing/batches/date=YYYY-MM-DD/batch_*.parquet" && exit 1)
	$(PYTHON) data/ingest/ingest.py "$(BATCH)"

detect-drift:
	@test -n "$(BATCH)" || (echo "Usage: make detect-drift BATCH=data/landing/batches/date=YYYY-MM-DD/batch_*.parquet" && exit 1)
	$(PYTHON) data/drift/detector.py "$(BATCH)"

refresh-reference:
	@test -n "$(MODEL)" || (echo "Usage: make refresh-reference MODEL=v4" && exit 1)
	$(PYTHON) data/drift/detector.py --refresh-reference $(MODEL)

realised-performance:
	$(PYTHON) data/drift/realised_performance.py

# ---------------------------------------------------------------- platform
simulate:
	$(PYTHON) scripts/simulate_walking_skeleton.py --scenario $(SCENARIO) --severity $(SEVERITY)

test: test-contracts test-unit test-llm test-e2e

test-unit:
	$(PYTHON) -m pytest tests/unit -q

test-contracts:
	$(PYTHON) tests/integration/test_contracts.py

test-llm:
	$(PYTHON) llm/evals/eval_runner.py

test-e2e:
	$(PYTHON) tests/integration/test_e2e_walkthrough.py

lint:
	ruff check .
	mypy --ignore-missing-imports data/ ml/ llm/ ops/

terraform-bootstrap:
	gcloud storage buckets create gs://$(PROJECT_ID)-tfstate --project=$(PROJECT_ID) --location=$(REGION) --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=0s
	gcloud storage buckets update gs://$(PROJECT_ID)-tfstate --versioning

terraform-init:
	cd infra/envs/dev && terraform init -migrate-state -force-copy -backend-config="bucket=$(PROJECT_ID)-tfstate" -backend-config="prefix=terraform/state"

terraform-plan: terraform-init
	cd infra/envs/dev && terraform plan -var="project_id=$(PROJECT_ID)"
clean:
	rm -rf data/landing data/quarantine data/warehouse data/events_outbox data/predictions_log data/llm_calls \
		reports/ artifacts/models/v-* .pytest_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
