terraform {
  required_version = ">= 1.5.0"
  backend "gcs" {}
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.15.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable APIs
module "apis" {
  source     = "../../modules/apis"
  project_id = var.project_id
}

# 2. Storage Buckets (landing, quarantine, artifacts, reports)
module "storage" {
  source     = "../../modules/storage"
  project_id = var.project_id
  region     = var.region
  depends_on = [module.apis]
}

# 3. BigQuery Dataset & Tables from Schemas
module "bigquery" {
  source     = "../../modules/bigquery"
  project_id = var.project_id
  region     = var.region
  depends_on = [module.apis]
}

# 4. Pub/Sub Topics & Subscriptions
module "pubsub" {
  source     = "../../modules/pubsub"
  project_id = var.project_id
  depends_on = [module.apis]
}

# 5. Artifact Registry for Container Images
module "artifact_registry" {
  source     = "../../modules/artifact_registry"
  project_id = var.project_id
  region     = var.region
  depends_on = [module.apis]
}

# 6. Cloud Run Services & IAM
module "cloud_run" {
  source                = "../../modules/cloud_run_service"
  project_id            = var.project_id
  region                = var.region
  image_tag             = var.image_tag
  service_account_emails = module.iam.service_accounts
  depends_on            = [module.apis, module.artifact_registry, module.iam]
}

# 7. Secrets
module "secrets" {
  source     = "../../modules/secrets"
  project_id = var.project_id
  depends_on = [module.apis]
}

# 8. Monitoring dashboards and SLO / dead-letter alerts
module "monitoring" {
  source                = "../../modules/monitoring"
  project_id            = var.project_id
  notification_channels = var.notification_channels
  depends_on            = [module.apis, module.cloud_run, module.pubsub]
}
# 9. Component identities and resource-scoped permissions
module "iam" {
  source     = "../../modules/iam"
  project_id = var.project_id
  dataset_id = module.bigquery.dataset_id
  bucket_names = {
    landing    = module.storage.landing_bucket
    quarantine = module.storage.quarantine_bucket
    artifacts  = module.storage.artifacts_bucket
    reports    = module.storage.reports_bucket
  }
  topic_ids       = module.pubsub.topic_ids
  slack_secret_id = module.secrets.slack_secret_id
  depends_on      = [module.apis]
}
