"""
Cloud Run entrypoint for the drift service (Person 1, Phase D).

Push subscriptions deliver base64 Pub/Sub envelopes to POST /; the service routes
on event_type:

  data.batch.landed -> score the batch and, if critical, publish drift.detected
  model.promoted    -> refresh the reference window to the new champion's data

Ack semantics matter: a bad payload returns 204 (drop it, the dead-letter topic is
for transport failures, not for messages we can never process), while an internal
error returns 500 so Pub/Sub retries.
"""

from __future__ import annotations

import base64
import json
import os
import sys

from fastapi import FastAPI, Request, Response

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.logging_utils import get_logger  # noqa: E402
from detector import handle_model_promoted, run_drift_analysis  # noqa: E402

log = get_logger("drift-detector")
app = FastAPI(title="ChurnOps Drift Detector", version="2.0.0")


def decode_push(envelope: dict) -> dict:
    """Unwrap a Pub/Sub push envelope into the event payload."""
    message = envelope.get("message", envelope)
    data = message.get("data")
    if data:
        return json.loads(base64.b64decode(data).decode("utf-8"))
    return message


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "drift-detector"}


@app.post("/")
async def handle_push(request: Request) -> Response:
    try:
        envelope = await request.json()
        event = decode_push(envelope)
    except Exception as exc:
        log.error(f"Unparseable push message dropped: {exc}", "tr-unset")
        return Response(status_code=204)

    event_type = event.get("event_type", "")
    trace_id = event.get("trace_id", "tr-unset")

    try:
        if event_type == "data.batch.landed":
            result = run_drift_analysis(current_batch_path=event["gcs_uri"])
            return Response(
                content=json.dumps({"severity": result.get("severity"), "batch_id": result.get("batch_id")}),
                media_type="application/json",
            )

        if event_type == "model.promoted":
            result = handle_model_promoted(event)
            return Response(content=json.dumps(result, default=str), media_type="application/json")

        log.info(f"Ignoring event type {event_type}", trace_id, event_type=event_type)
        return Response(status_code=204)

    except KeyError as exc:
        log.error(f"Event {event_type} missing required field {exc}", trace_id, event_type=event_type)
        return Response(status_code=204)
    except Exception as exc:  # retryable
        log.exception(f"Failed to handle {event_type}", exc, trace_id, event_type=event_type)
        return Response(status_code=500)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
