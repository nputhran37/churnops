locals {
  dashboard_files = fileset("${path.module}/../../../ops/dashboards", "*_dashboard.json")
}

resource "google_monitoring_dashboard" "churnops" {
  for_each       = toset(local.dashboard_files)
  project        = var.project_id
  dashboard_json = file("${path.module}/../../../ops/dashboards/${each.value}")
}

resource "google_monitoring_alert_policy" "serving_latency" {
  project               = var.project_id
  display_name          = "ChurnOps serving p95 latency above 300 ms"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.notification_channels

  conditions {
    display_name = "Serving p95 latency exceeds 300 ms for five minutes"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_latencies\" AND resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"churnops-serving\""
      comparison      = "COMPARISON_GT"
      threshold_value = 300
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "ChurnOps serving latency has exceeded the 300 ms p95 target. Check the serving revision and Cloud Run request logs."
  }
}

resource "google_monitoring_alert_policy" "dead_letter_messages" {
  project               = var.project_id
  display_name          = "ChurnOps dead-letter queue has undelivered messages"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.notification_channels

  conditions {
    display_name = "At least one message is waiting in the dead-letter subscription"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.type=\"pubsub_subscription\" AND resource.label.\"subscription_id\"=\"churnops-dead-letter-sub\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "ChurnOps has undelivered messages in its dead-letter subscription. Inspect the event payload and subscriber logs before replaying messages."
  }
}