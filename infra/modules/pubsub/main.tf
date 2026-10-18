variable "project_id" {
  type = string
}

locals {
  topics = [
    "data.batch.landed",
    "drift.detected",
    "model.trained",
    "model.promoted",
    "model.rejected"
  ]
}

# Dead Letter Topic
resource "google_pubsub_topic" "dlq" {
  name    = "churnops-dead-letter"
  project = var.project_id
}

resource "google_pubsub_subscription" "dlq_sub" {
  name    = "churnops-dead-letter-sub"
  topic   = google_pubsub_topic.dlq.name
  project = var.project_id
}

# Core Event Topics
resource "google_pubsub_topic" "topics" {
  for_each = toset(local.topics)
  name     = each.key
  project  = var.project_id
}

# Subscriptions with Dead Letter Topic protection
resource "google_pubsub_subscription" "subs" {
  for_each = toset(local.topics)
  name     = "${each.key}-sub"
  topic    = google_pubsub_topic.topics[each.key].name
  project  = var.project_id

  ack_deadline_seconds = 60

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dlq.id
    max_delivery_attempts = 5
  }
}

output "topic_ids" {
  value = { for k, v in google_pubsub_topic.topics : k => v.id }
}
