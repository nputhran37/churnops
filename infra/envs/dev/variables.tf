variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = "churnops-512010"
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

variable "notification_channels" {
  description = "Optional Cloud Monitoring notification channel resource names for alerts."
  type        = list(string)
  default     = []
}