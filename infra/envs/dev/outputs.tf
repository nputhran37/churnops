output "landing_bucket" {
  value = module.storage.landing_bucket
}

output "quarantine_bucket" {
  value = module.storage.quarantine_bucket
}

output "artifacts_bucket" {
  value = module.storage.artifacts_bucket
}

output "reports_bucket" {
  value = module.storage.reports_bucket
}

output "bigquery_dataset" {
  value = module.bigquery.dataset_id
}

output "pubsub_topics" {
  value = module.pubsub.topic_ids
}

output "serving_url" {
  value = module.cloud_run.serving_url
}

output "analyst_url" {
  value = module.cloud_run.analyst_url
}

output "monitoring_dashboard_ids" {
  value = module.monitoring.dashboard_ids
}

output "monitoring_alert_policy_ids" {
  value = module.monitoring.alert_policy_ids
}