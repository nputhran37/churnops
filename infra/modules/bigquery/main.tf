variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

resource "google_bigquery_dataset" "churnops" {
  dataset_id  = "churnops"
  project     = var.project_id
  location    = var.region
  description = "ChurnOps operational tables, features, logs, and telemetry"
}

# 1. raw_events table (partitioned by batch_date, clustered by customer_segment)
resource "google_bigquery_table" "raw_events" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "raw_events"
  project    = var.project_id

  time_partitioning {
    type  = "DAY"
    field = "batch_date"
  }

  clustering = ["customer_segment"]
  schema     = file("${path.module}/../../../contracts/bq/raw_events.json")
}

# 2. features_daily table
resource "google_bigquery_table" "features_daily" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "features_daily"
  project    = var.project_id

  time_partitioning {
    type  = "DAY"
    field = "batch_date"
  }

  clustering = ["customer_segment"]
  schema     = file("${path.module}/../../../contracts/bq/features_daily.json")
}

# Delayed churn labels are stored separately so the model pipeline can replay
# realistic label availability without exposing labels on landing batches.
resource "google_bigquery_table" "labels" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "labels"
  project    = var.project_id

  time_partitioning {
    type  = "DAY"
    field = "batch_date"
  }

  schema = file("${path.module}/../../../contracts/bq/labels.json")
}

# 3. reference_window table
resource "google_bigquery_table" "reference_window" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "reference_window"
  project    = var.project_id
  schema     = file("${path.module}/../../../contracts/bq/reference_window.json")
}

# 4. drift_reports table
resource "google_bigquery_table" "drift_reports" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "drift_reports"
  project    = var.project_id

  time_partitioning {
    type  = "DAY"
    field = "batch_date"
  }

  schema = file("${path.module}/../../../contracts/bq/drift_reports.json")
}

# 5. predictions_log table
resource "google_bigquery_table" "predictions_log" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "predictions_log"
  project    = var.project_id

  time_partitioning {
    type = "DAY"
  }

  schema = file("${path.module}/../../../contracts/bq/predictions_log.json")
}

# 6. llm_calls table
resource "google_bigquery_table" "llm_calls" {
  dataset_id = google_bigquery_dataset.churnops.dataset_id
  table_id   = "llm_calls"
  project    = var.project_id

  time_partitioning {
    type = "DAY"
  }

  schema = file("${path.module}/../../../contracts/bq/llm_calls.json")
}

output "dataset_id" {
  value = google_bigquery_dataset.churnops.dataset_id
}
