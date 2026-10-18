variable "project_id" {
  description = "GCP project that owns the ChurnOps monitoring resources."
  type        = string
}

variable "notification_channels" {
  description = "Optional Cloud Monitoring notification channel resource names."
  type        = list(string)
  default     = []
}