variable "project_id" {
  type = string
}

resource "google_secret_manager_secret" "slack_webhook" {
  secret_id = "slack-webhook-url"
  project   = var.project_id

  replication {
    auto {}
  }
}

output "slack_secret_id" {
  value = google_secret_manager_secret.slack_webhook.id
}
