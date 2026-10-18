variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

resource "google_artifact_registry_repository" "docker_repo" {
  location      = var.region
  repository_id = "churnops-docker"
  description   = "Docker container registry for ChurnOps microservices"
  format        = "DOCKER"
  project       = var.project_id

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-minimum-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
}

output "repository_name" {
  value = google_artifact_registry_repository.docker_repo.name
}
