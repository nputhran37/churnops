"""
Structured JSON logging per contracts/logging.md.

Every data-plane service logs one JSON object per line to stdout so Cloud Logging
parses it natively and a single trace_id can be filtered across the whole flow.
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from typing import Any

_VALID_SEVERITIES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StructuredLogger:
    """Emits contracts/logging.md compliant single-line JSON logs."""

    def __init__(self, service: str, stream=None) -> None:
        self.service = service
        self._logger = logging.getLogger(f"churnops.{service}")
        if not self._logger.handlers:
            # stderr keeps machine-readable job output on stdout; Cloud Logging
            # still parses the JSON and honours the "severity" field.
            handler = logging.StreamHandler(stream or sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def log(self, severity: str, message: str, trace_id: str = "tr-unset", **fields: Any) -> dict:
        severity = severity.upper()
        if severity not in _VALID_SEVERITIES:
            severity = "INFO"
        payload = {
            "timestamp": _utc_now(),
            "severity": severity,
            "service": self.service,
            "message": message,
            "trace_id": trace_id,
        }
        payload.update({k: v for k, v in fields.items() if v is not None})
        self._logger.info(json.dumps(payload, default=str))
        return payload

    @staticmethod
    def _safe(fields: dict) -> dict:
        """Callers often log a domain 'severity' (drift severity); keep it from colliding."""
        if "severity" in fields:
            fields = dict(fields)
            fields["event_severity"] = fields.pop("severity")
        return fields

    def info(self, message: str, trace_id: str = "tr-unset", **fields: Any) -> dict:
        return self.log("INFO", message, trace_id, **self._safe(fields))

    def warning(self, message: str, trace_id: str = "tr-unset", **fields: Any) -> dict:
        return self.log("WARNING", message, trace_id, **self._safe(fields))

    def error(self, message: str, trace_id: str = "tr-unset", **fields: Any) -> dict:
        return self.log("ERROR", message, trace_id, **self._safe(fields))

    def exception(self, message: str, exc: BaseException, trace_id: str = "tr-unset", **fields: Any) -> dict:
        return self.log(
            "ERROR",
            message,
            trace_id,
            error_details={"type": type(exc).__name__, "message": str(exc)[:500]},
            **fields,
        )


def get_logger(service: str) -> StructuredLogger:
    return StructuredLogger(service)
