output "dashboard_ids" {
  description = "Cloud Monitoring dashboard resource names managed by this module."
  value       = { for file, dashboard in google_monitoring_dashboard.churnops : file => dashboard.id }
}

output "alert_policy_ids" {
  description = "Cloud Monitoring alert policy resource names managed by this module."
  value = {
    serving_latency      = google_monitoring_alert_policy.serving_latency.name
    dead_letter_messages = google_monitoring_alert_policy.dead_letter_messages.name
  }
}