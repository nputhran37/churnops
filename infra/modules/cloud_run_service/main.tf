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

# 1. Service Account for Serving
resource "google_service_account" "sa_serving" {
  account_id   = "sa-serving"
  display_name = "ChurnOps Serving Service Account"
  project      = var.project_id
}

# 2. Service Account for Drift Analyst
resource "google_service_account" "sa_analyst" {
  account_id   = "sa-analyst"
  display_name = "ChurnOps Drift Analyst Service Account"
  project      = var.project_id
}

# IAM: Vertex AI User for Gemini
resource "google_project_iam_member" "analyst_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.sa_analyst.email}"
}

# Cloud Run Serving Service
resource "google_cloud_run_v2_service" "serving" {
  name     = "churnops-serving"
  location = var.region
  project  = var.project_id

  template {
    service_account = google_service_account.sa_serving.email
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
    service_account = google_service_account.sa_analyst.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/churnops-docker/analyst:${var.image_tag}"
      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }
    }
  }
}

output "serving_url" {
  value = google_cloud_run_v2_service.serving.uri
}

output "analyst_url" {
  value = google_cloud_run_v2_service.analyst.uri
}
