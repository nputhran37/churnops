variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "service_account_emails" {
  description = "Service account emails keyed by component name."
  type        = map(string)
}

# Cloud Run Serving Service
resource "google_cloud_run_v2_service" "serving" {
  name     = "churnops-serving"
  location = var.region
  project  = var.project_id

  template {
    service_account = var.service_account_emails["serving"]
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/churnops-docker/serving:${var.image_tag}"
      resources {
        limits = {
          cpu    = "1000m"
          memory = "1024Mi"
        }
      }
      env {
        name  = "MODEL_ALIAS"
        value = "champion"
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# Cloud Run Analyst Service
resource "google_cloud_run_v2_service" "analyst" {
  name     = "churnops-analyst"
  location = var.region
  project  = var.project_id

  template {
    service_account = var.service_account_emails["analyst"]
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/churnops-docker/analyst:${var.image_tag}"
      resources {
        limits = {
          cpu    = "1000m"
          memory = "1Gi"
        }
      }
      env {
        name  = "GEMINI_MODE"
        value = "vertex"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GEMINI_MODEL"
        value = "gemini-2.5-flash"
      }
    }
  }
}

# Cloud Run Drift Detector. Pub/Sub push subscriptions call this service after
# ingest lands a validated batch and after a model is promoted.
resource "google_cloud_run_v2_service" "drift" {
  name     = "churnops-drift"
  location = var.region
  project  = var.project_id

  template {
    service_account = var.service_account_emails["drift"]
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/churnops-docker/drift:${var.image_tag}"
      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }
      env {
        name  = "CHURNOPS_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CHURNOPS_BQ_DATASET"
        value = "churnops"
      }
      env {
        name  = "CHURNOPS_USE_GCS"
        value = "1"
      }
      env {
        name  = "CHURNOPS_USE_BQ"
        value = "1"
      }
      env {
        name  = "CHURNOPS_USE_PUBSUB"
        value = "1"
      }
    }
  }
}

# Authenticated Pub/Sub push endpoint that launches Vertex AI training runs.
resource "google_cloud_run_v2_service" "trigger" {
  name     = "churnops-trigger"
  location = var.region
  project  = var.project_id

  template {
    service_account = var.service_account_emails["trigger"]
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/churnops-docker/trigger:${var.image_tag}"
      resources {
        limits = {
          cpu    = "1000m"
          memory = "1Gi"
        }
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "PIPELINE_ROOT"
        value = "gs://${var.project_id}-artifacts/pipelines"
      }
      env {
        name  = "ARTIFACTS_BUCKET"
        value = "${var.project_id}-artifacts"
      }
      env {
        name  = "PIPELINE_SERVICE_ACCOUNT"
        value = var.service_account_emails["pipeline"]
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "trigger_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.trigger.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails["trigger"]}"
}

resource "google_cloud_run_v2_service_iam_member" "drift_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.drift.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails["drift"]}"
}

resource "google_cloud_run_v2_service_iam_member" "analyst_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.analyst.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails["analyst"]}"
}

output "serving_url" {
  value = google_cloud_run_v2_service.serving.uri
}

output "analyst_url" {
  value = google_cloud_run_v2_service.analyst.uri
}

output "trigger_url" {
  value = google_cloud_run_v2_service.trigger.uri
}

output "drift_url" {
  value = google_cloud_run_v2_service.drift.uri
}
