"""Idempotent Pub/Sub to Vertex AI Pipelines dispatcher.

The event's trace ID is the idempotency key. GCS generation preconditions make
the claim safe across independent Cloud Run/Functions instances.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import functions_framework
from google.api_core.exceptions import PreconditionFailed
from google.cloud import aiplatform, storage

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("trigger-fn")

PROJECT_ID = os.environ["PROJECT_ID"]
REGION = os.getenv("REGION", "us-central1")
PIPELINE_ROOT = os.getenv("PIPELINE_ROOT", f"gs://{PROJECT_ID}-artifacts/pipelines")
PIPELINE_SPEC_URI = os.getenv("PIPELINE_SPEC_URI", f"{PIPELINE_ROOT}/churn_retraining_latest.yaml")
ARTIFACTS_BUCKET = os.getenv("ARTIFACTS_BUCKET", f"{PROJECT_ID}-artifacts")
PIPELINE_SERVICE_ACCOUNT = os.getenv("PIPELINE_SERVICE_ACCOUNT")
LOCK_TTL_HOURS = int(os.getenv("LOCK_TTL_HOURS", "12"))


def _payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    message = data.get("message", {})
    encoded = message.get("data") if isinstance(message, dict) else None
    if encoded:
        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Pub/Sub event data must decode to a JSON object")
        return decoded
    return data


def _claim(bucket, key: str, body: dict[str, Any]) -> bool:
    """Create a durable idempotency record once; return False on duplicates."""
    blob = bucket.blob(key)
    try:
        blob.upload_from_string(json.dumps(body), if_generation_match=0,
                                content_type="application/json")
        return True
    except PreconditionFailed:
        return False


def _acquire_run_lock(bucket, trace_id: str) -> bool:
    """Acquire a cross-instance lock, reclaiming only an expired lock."""
    blob = bucket.blob("pipeline-locks/active.json")
    now = datetime.now(timezone.utc)
    lock = {"trace_id": trace_id, "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=LOCK_TTL_HOURS)).isoformat()}
    try:
        blob.upload_from_string(json.dumps(lock), if_generation_match=0,
                                content_type="application/json")
        return True
    except PreconditionFailed:
        blob.reload()
        current = json.loads(blob.download_as_text())
        if datetime.fromisoformat(current["expires_at"]) > now:
            if current.get("trace_id") == trace_id:
                return True
            # A previous pipeline may have finished before the lease expires.
            # Reclaim only when Vertex confirms a terminal state.
            previous_trace = current.get("trace_id")
            try:
                previous = json.loads(bucket.blob(
                    f"pipeline-runs/claims/{previous_trace}.json"
                ).download_as_text())
                if previous.get("run_id"):
                    previous_job = aiplatform.PipelineJob.get(previous["run_id"])
                    state = str(previous_job.state)
                    if any(done in state for done in ("SUCCEEDED", "FAILED", "CANCELLED")):
                        blob.delete(if_generation_match=int(blob.generation))
                        return _acquire_run_lock(bucket, trace_id)
            except Exception:
                logger.exception("Could not check active Vertex run; retaining lock")
            return False
        try:
            blob.upload_from_string(json.dumps(lock), if_generation_match=int(blob.generation),
                                    content_type="application/json")
            return True
        except PreconditionFailed:
            return False


def _release_run_lock(bucket, trace_id: str) -> None:
    blob = bucket.blob("pipeline-locks/active.json")
    try:
        blob.reload()
        current = json.loads(blob.download_as_text())
        if current.get("trace_id") == trace_id:
            blob.delete(if_generation_match=int(blob.generation))
    except Exception:
        logger.exception("Unable to release pipeline lock for trace_id=%s", trace_id)


@functions_framework.http
def launch_retraining_pipeline(request):
    """HTTP target for an authenticated Pub/Sub push subscription."""
    if request.method == "GET":
        return {"status": "ok", "service": "churnops-trigger"}, 200
    envelope = request.get_json(silent=True) or {}
    try:
        event = _payload(envelope)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        # Invalid or legacy messages cannot succeed on retry. Ack them so they
        # do not block later valid drift incidents on the push subscription.
        logger.warning("Dropping malformed Pub/Sub event: %s", exc)
        return {"status": "ignored", "reason": "malformed_pubsub_event"}, 200
    if event.get("event_type") != "drift.detected":
        return {"status": "ignored", "reason": "unsupported_event_type"}
    trace_id = str(event.get("trace_id", "")).strip()
    if not trace_id:
        raise ValueError("drift.detected event is missing trace_id")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", trace_id):
        raise ValueError("trace_id contains unsupported characters")

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(ARTIFACTS_BUCKET)
    # The SDK's staging_bucket argument must be the bucket root; PIPELINE_ROOT
    # is a subdirectory reserved for pipeline output artifacts.
    aiplatform.init(
        project=PROJECT_ID,
        location=REGION,
        staging_bucket=f"gs://{ARTIFACTS_BUCKET}",
    )
    record_key = f"pipeline-runs/claims/{trace_id}.json"
    record = {"trace_id": trace_id, "batch_id": event.get("batch_id"),
              "status": "CLAIMED", "created_at": datetime.now(timezone.utc).isoformat()}
    if not _claim(bucket, record_key, record):
        return {"status": "skipped", "reason": "idempotent_duplicate", "trace_id": trace_id}
    if not _acquire_run_lock(bucket, trace_id):
        # Keep a durable deferred record so Pub/Sub redelivery can retry later.
        bucket.blob(record_key).delete()
        raise RuntimeError("another Vertex training pipeline is active; retry this event")

    try:
        run_id = f"ct-{re.sub(r'[^a-z0-9-]', '-', trace_id.lower())[:45]}"
        job = aiplatform.PipelineJob(
            display_name=run_id,
            template_path=PIPELINE_SPEC_URI,
            pipeline_root=PIPELINE_ROOT,
            parameter_values={
                "snapshot_date": str(event.get("batch_date") or datetime.now(timezone.utc).date()),
                "drift_trace_id": trace_id,
                "trigger_reason": "drift.detected",
            },
            labels={"churnops_trace": re.sub(r"[^a-z0-9_-]", "-", trace_id.lower())[:63]},
            enable_caching=False,
        )
        job.submit(service_account=PIPELINE_SERVICE_ACCOUNT)
        record.update({"status": "SUBMITTED", "run_id": job.resource_name,
                       "submitted_at": datetime.now(timezone.utc).isoformat()})
        bucket.blob(record_key).upload_from_string(json.dumps(record),
                                                   content_type="application/json")
        logger.info(json.dumps({"service": "trigger_fn", **record}))
        return {"status": "dispatched", "run_id": job.resource_name, "trace_id": trace_id}
    except Exception as exc:
        bucket.blob(record_key).delete()
        _release_run_lock(bucket, trace_id)
        logger.exception("Vertex pipeline submission failed for trace_id=%s", trace_id)
        raise RuntimeError(f"Vertex pipeline submission failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit("Run this service with functions-framework; use the unit tests for local checks.")
