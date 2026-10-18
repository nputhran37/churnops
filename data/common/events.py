"""
Pub/Sub event publishing for the data plane.

Every payload is validated against contracts/events/<event_type>.json *before* it
leaves the process, so a producer can never put a message on the bus that its
consumers (P2's analyst, P3's trigger function) would reject. Without GCP
credentials the message is written to a local outbox instead, which is what the
offline walking skeleton and the tests read.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from dataclasses import dataclass

import jsonschema

from .contracts import load_event_schema
from .logging_utils import get_logger

SCHEMA_VERSION = "1.0.0"
DEFAULT_OUTBOX = os.path.join("data", "events_outbox")

_log = get_logger("events")


class EventValidationError(ValueError):
    """Raised when a payload does not satisfy its contracts/events schema."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_trace_id(prefix: str = "tr") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def validate_event(event_type: str, payload: dict) -> dict:
    schema = load_event_schema(event_type)
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:
        raise EventValidationError(
            f"{event_type} payload violates its contract at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"
        ) from exc
    return payload


def build_event(event_type: str, **fields) -> dict:
    """Assemble a payload with the envelope fields every ChurnOps event carries."""
    payload = {
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "emitted_at": utc_now(),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    return validate_event(event_type, payload)


@dataclass
class EventPublisher:
    outbox_dir: str = DEFAULT_OUTBOX
    project: str = os.getenv("CHURNOPS_PROJECT_ID", "churnops-dev")
    remote: bool = os.getenv("CHURNOPS_USE_PUBSUB", "0").lower() in ("1", "true", "yes")
    service: str = "data-plane"

    def topic_path(self, event_type: str) -> str:
        """Topic names mirror event names with dots replaced by dashes."""
        return f"projects/{self.project}/topics/{event_type.replace('.', '-')}"

    def publish(self, event_type: str, payload: dict, trace_id: str | None = None) -> dict:
        validate_event(event_type, payload)
        trace_id = trace_id or payload.get("trace_id", "tr-unset")

        if self.remote:  # pragma: no cover - requires GCP
            from google.cloud import pubsub_v1

            publisher = pubsub_v1.PublisherClient()
            future = publisher.publish(
                self.topic_path(event_type),
                json.dumps(payload).encode("utf-8"),
                event_type=event_type,
                trace_id=trace_id,
                schema_version=payload.get("schema_version", SCHEMA_VERSION),
            )
            message_id = future.result(timeout=30)
            _log.info(
                f"Published {event_type} to Pub/Sub",
                trace_id,
                event_type=event_type,
                message_id=message_id,
                topic=self.topic_path(event_type),
            )
            return {"transport": "pubsub", "message_id": message_id, "payload": payload}

        os.makedirs(self.outbox_dir, exist_ok=True)
        slug = event_type.replace(".", "_")
        key = payload.get("batch_id") or payload.get("model_version") or uuid.uuid4().hex[:8]
        path = os.path.join(self.outbox_dir, f"{slug}_{key}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

        _log.info(
            f"Published {event_type} to local outbox",
            trace_id,
            event_type=event_type,
            outbox_path=path,
            topic=self.topic_path(event_type),
        )
        return {"transport": "outbox", "path": path, "payload": payload}


def read_outbox(event_type: str, outbox_dir: str = DEFAULT_OUTBOX) -> list[dict]:
    """Read locally published events of one type (used by tests and the demo)."""
    slug = event_type.replace(".", "_")
    if not os.path.isdir(outbox_dir):
        return []
    payloads = []
    for entry in sorted(os.listdir(outbox_dir)):
        if entry.startswith(slug) and entry.endswith(".json"):
            with open(os.path.join(outbox_dir, entry), "r", encoding="utf-8") as handle:
                payloads.append(json.load(handle))
    return payloads
