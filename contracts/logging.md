# ChurnOps Structured Logging & Observability Standard

All microservices (Generator, Ingest, Drift, Serving, Analyst, Trigger Function) emit single-line structured JSON logs to standard output. Cloud Logging automatically parses these fields.

## Core Schema Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `timestamp` | ISO-8601 String | Yes | UTC event timestamp |
| `severity` | String | Yes | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `service` | String | Yes | Name of component (e.g. `generator`, `drift-detector`, `serving`, `analyst`) |
| `message` | String | Yes | Human-readable log entry |
| `trace_id` | String | Yes | Distributed correlation ID across the entire batch lifecycle |
| `batch_id` | String | No | Ingestion batch ID |
| `model_version`| String | No | Model version being served or evaluated |
| `latency_ms` | Float | No | Operation duration |
| `error_details`| Object | No | Exception type, message, and stack trace |

## Sample Structured Log Entry

```json
{
  "timestamp": "2026-10-02T14:30:05.123Z",
  "severity": "WARNING",
  "service": "drift-detector",
  "message": "Feature drift detected on MonthlyCharges (PSI=0.284)",
  "trace_id": "tr-20261002-demo-001",
  "batch_id": "b-20261002-01",
  "metrics": {
    "feature": "monthly_charges",
    "psi": 0.284,
    "threshold": 0.20
  }
}
```
