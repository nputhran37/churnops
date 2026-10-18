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
  source                 = "../../modules/cloud_run_service"
  project_id             = var.project_id
  region                 = var.region
  image_tag              = var.image_tag
  service_account_emails = module.iam.service_accounts
  depends_on             = [module.apis, module.artifact_registry, module.iam]
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

data "google_project" "current" {
  project_id = var.project_id
}

# Authenticated push delivery from drift detection to the Vertex dispatcher.
resource "google_service_account_iam_member" "pubsub_push_token_creator" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${module.iam.service_accounts["trigger"]}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "pubsub_drift_token_creator" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${module.iam.service_accounts["drift"]}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "pubsub_analyst_token_creator" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${module.iam.service_accounts["analyst"]}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# The dispatcher submits Vertex Pipeline jobs using sa-pipeline as the runtime
# identity, so the Cloud Run trigger identity must be allowed to act as it.
resource "google_service_account_iam_member" "trigger_act_as_pipeline" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${module.iam.service_accounts["pipeline"]}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${module.iam.service_accounts["trigger"]}"
}

resource "google_storage_bucket_iam_member" "trigger_bucket_reader" {
  bucket = module.storage.artifacts_bucket
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${module.iam.service_accounts["trigger"]}"
}

# Deliver validated batch events to the GCP drift detector.
resource "google_pubsub_subscription" "data_batch_landed_drift" {
  name    = "data-batch-landed-drift"
  topic   = module.pubsub.topic_ids["data-batch-landed"]
  project = var.project_id

  ack_deadline_seconds = 60
  push_config {
    push_endpoint = module.cloud_run.drift_url
    oidc_token {
      service_account_email = module.iam.service_accounts["drift"]
      audience              = module.cloud_run.drift_url
    }
  }
  dead_letter_policy {
    dead_letter_topic     = "projects/${var.project_id}/topics/churnops-dead-letter"
    max_delivery_attempts = 5
  }
  depends_on = [
    google_service_account_iam_member.pubsub_drift_token_creator,
    module.cloud_run,
  ]
}

# Keep the reference window in sync with a model promotion event.
resource "google_pubsub_subscription" "model_promoted_drift" {
  name    = "model-promoted-drift"
  topic   = module.pubsub.topic_ids["model-promoted"]
  project = var.project_id

  ack_deadline_seconds = 60
  push_config {
    push_endpoint = module.cloud_run.drift_url
    oidc_token {
      service_account_email = module.iam.service_accounts["drift"]
      audience              = module.cloud_run.drift_url
    }
  }
  dead_letter_policy {
    dead_letter_topic     = "projects/${var.project_id}/topics/churnops-dead-letter"
    max_delivery_attempts = 5
  }
  depends_on = [
    google_service_account_iam_member.pubsub_drift_token_creator,
    module.cloud_run,
  ]
}

# The cloud analyst receives the same validated drift event as the Vertex trigger.
resource "google_pubsub_subscription" "drift_detected_analyst" {
  name    = "drift-detected-analyst"
  topic   = module.pubsub.topic_ids["drift-detected"]
  project = var.project_id

  ack_deadline_seconds = 60
  push_config {
    push_endpoint = module.cloud_run.analyst_url
    oidc_token {
      service_account_email = module.iam.service_accounts["analyst"]
      audience              = module.cloud_run.analyst_url
    }
  }
  dead_letter_policy {
    dead_letter_topic     = "projects/${var.project_id}/topics/churnops-dead-letter"
    max_delivery_attempts = 5
  }
  depends_on = [
    google_service_account_iam_member.pubsub_analyst_token_creator,
    module.cloud_run,
  ]
}

resource "google_pubsub_subscription" "training_trigger" {
  name    = "drift-detected-trigger"
  topic   = module.pubsub.topic_ids["drift-detected"]
  project = var.project_id

  ack_deadline_seconds = 60
  push_config {
    push_endpoint = module.cloud_run.trigger_url
    oidc_token {
      service_account_email = module.iam.service_accounts["trigger"]
      audience              = module.cloud_run.trigger_url
    }
  }
  dead_letter_policy {
    dead_letter_topic     = "projects/${var.project_id}/topics/churnops-dead-letter"
    max_delivery_attempts = 5
  }
  depends_on = [google_service_account_iam_member.pubsub_push_token_creator]
}
