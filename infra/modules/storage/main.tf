variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

# 1. Landing Bucket
resource "google_storage_bucket" "landing" {
  name                        = "${var.project_id}-landing"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}

# 2. Quarantine Bucket
resource "google_storage_bucket" "quarantine" {
  name                        = "${var.project_id}-quarantine"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 60
    }
    action {
      type = "Delete"
    }
  }
}

# 3. Artifacts Bucket
resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  force_destroy = false
}

# 4. Reports Bucket
resource "google_storage_bucket" "reports" {
  name                        = "${var.project_id}-reports"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

output "landing_bucket" {
  value = google_storage_bucket.landing.name
}

output "quarantine_bucket" {
  value = google_storage_bucket.quarantine.name
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "reports_bucket" {
  value = google_storage_bucket.reports.name
}
