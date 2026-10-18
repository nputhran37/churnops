variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = "churnops-dev-project"
}

variable "region" {
  type        = string
  description = "Default GCP Region"
  default     = "us-central1"
}

variable "image_tag" {
  type        = string
  description = "Container image tag (e.g. git SHA or latest)"
  default     = "latest"
}
