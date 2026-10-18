variable "project_id" {
  description = "GCP project that owns ChurnOps service accounts."
  type        = string
}

variable "dataset_id" {
  description = "BigQuery dataset used by ChurnOps services."
  type        = string
}

variable "bucket_names" {
  description = "Bucket names keyed by landing, quarantine, artifacts, and reports."
  type        = map(string)
}

variable "topic_ids" {
  description = "Fully qualified Pub/Sub topic names keyed by event name."
  type        = map(string)
}

variable "slack_secret_id" {
  description = "Full resource name of the Slack webhook secret."
  type        = string
}

locals {
  components = toset(["generator", "ingest", "drift", "pipeline", "serving", "analyst", "trigger", "cicd"])

  vertex_roles = ["pipeline", "analyst", "trigger"]

  dataset_editors = ["generator", "ingest", "drift", "pipeline", "analyst"]

  storage_grants = {
    "generator-landing" = { component = "generator", bucket = "landing", role = "roles/storage.objectAdmin" }
    "ingest-landing"    = { component = "ingest", bucket = "landing", role = "roles/storage.objectViewer" }
    "ingest-quarantine" = { component = "ingest", bucket = "quarantine", role = "roles/storage.objectAdmin" }
    "drift-landing"     = { component = "drift", bucket = "landing", role = "roles/storage.objectViewer" }
    "drift-reports"     = { component = "drift", bucket = "reports", role = "roles/storage.objectAdmin" }
    "pipeline-artifacts" = { component = "pipeline", bucket = "artifacts", role = "roles/storage.objectAdmin" }
    "serving-artifacts" = { component = "serving", bucket = "artifacts", role = "roles/storage.objectViewer" }
    "analyst-reports"   = { component = "analyst", bucket = "reports", role = "roles/storage.objectViewer" }
    "trigger-artifacts" = { component = "trigger", bucket = "artifacts", role = "roles/storage.objectAdmin" }
  }

  topic_grants = {
    "ingest-batch-landed" = { component = "ingest", topic = "data-batch-landed" }
    "drift-detected"      = { component = "drift", topic = "drift-detected" }
    "pipeline-trained"    = { component = "pipeline", topic = "model-trained" }
    "pipeline-rejected"   = { component = "pipeline", topic = "model-rejected" }
    "cicd-promoted"       = { component = "cicd", topic = "model-promoted" }
    "cicd-rejected"       = { component = "cicd", topic = "model-rejected" }
  }

}

resource "google_service_account" "component" {
  for_each     = local.components
  project      = var.project_id
  account_id   = "sa-${each.value}"
  display_name = "ChurnOps ${each.value} service account"
}

resource "google_project_iam_member" "job_user" {
  for_each = toset(["generator", "ingest", "drift", "pipeline", "analyst", "serving"])
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.component[each.key].email}"
}

resource "google_project_iam_member" "vertex_user" {
  for_each = toset(local.vertex_roles)
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.component[each.key].email}"
}

resource "google_bigquery_dataset_iam_member" "dataset_editor" {
  for_each   = toset(local.dataset_editors)
  project    = var.project_id
  dataset_id = var.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.component[each.key].email}"
}

resource "google_storage_bucket_iam_member" "bucket_access" {
  for_each = local.storage_grants
  bucket   = var.bucket_names[each.value.bucket]
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.component[each.value.component].email}"
}

resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = local.topic_grants
  project  = var.project_id
  topic    = var.topic_ids[each.value.topic]
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:${google_service_account.component[each.value.component].email}"
}

resource "google_secret_manager_secret_iam_member" "analyst_secret_access" {
  project   = var.project_id
  secret_id = var.slack_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.component["analyst"].email}"
}

output "service_accounts" {
  description = "Service account email addresses keyed by component."
  value       = { for component, account in google_service_account.component : component => account.email }
}
