"""
ChurnOps drift detection service (Person 1, Phase D).

Runs as a Cloud Run service push-subscribed to data.batch.landed (see server.py)
and from the CLI for local runs:

  current batch + frozen reference_window
      -> PSI + KS per numeric feature, Jensen-Shannon + chi-square per categorical
      -> prediction drift against the champion's score distribution
      -> decision rule from drift_policy.yaml (none / warn / critical)
      -> report.json + report.html in gs://<project>-reports/drift/<batch_id>/
      -> a drift_reports row (realised_auc backfilled when labels arrive)
      -> drift.detected published when critical and the cooldown window is clear
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import sys

import pandas as pd
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from common.contracts import load_feature_contract  # noqa: E402
from common.events import EventPublisher, build_event  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402
from common.storage import Storage, read_tabular  # noqa: E402
from common.warehouse import Warehouse  # noqa: E402
from metrics import (  # noqa: E402
    FeatureDrift,
    ks_test,
    percent_change,
    score_categorical_feature,
    score_numeric_feature,
)
from prediction import collect_scores  # noqa: E402
from reference import ReferenceWindow, refresh_on_model_promoted  # noqa: E402

POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drift_policy.yaml")

log = get_logger("drift-detector")


def load_policy(path: str | None = None) -> dict:
    with open(path or POLICY_PATH, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ----------------------------------------------------------------------
# cooldown
# ----------------------------------------------------------------------
def cooldown_state(warehouse: Warehouse, policy: dict, severity: str, now: datetime.datetime | None = None) -> dict:
    """Decide whether a critical verdict may publish, given recent incidents."""
    settings = policy.get("cooldown", {})
    hours = float(settings.get("hours", 0))
    if hours <= 0 or severity != "critical":
        return {"suppressed": False, "last_incident_at": None, "cooldown_hours": hours}

    now = now or _utc_now()
    incidents = [
        row
        for row in warehouse.read_rows("drift_reports")
        if row.get("severity") == "critical" and row.get("published_event", True)
    ]
    if not incidents:
        return {"suppressed": False, "last_incident_at": None, "cooldown_hours": hours}

    def created(row: dict) -> datetime.datetime:
        try:
            value = datetime.datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        except ValueError:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)

    last = max(incidents, key=created)
    elapsed_hours = (now - created(last)).total_seconds() / 3600.0
    return {
        "suppressed": elapsed_hours < hours,
        "last_incident_at": last.get("created_at"),
        "last_incident_batch": last.get("batch_id"),
        "hours_since_last_incident": round(elapsed_hours, 2),
        "cooldown_hours": hours,
    }


# ----------------------------------------------------------------------
# decision
# ----------------------------------------------------------------------
def decide_severity(
    results: list[FeatureDrift],
    prediction_p_value: float | None,
    policy: dict,
    row_count: int,
) -> tuple[str, list[str]]:
    """Apply drift_policy.yaml to the measured statistics. Returns severity + reasons."""
    thresholds = policy["thresholds"]
    reasons: list[str] = []

    if row_count < int(policy.get("min_rows_for_verdict", 0)):
        return "none", [f"batch has only {row_count} rows; below min_rows_for_verdict"]

    top_priority = set(policy.get("top_priority_features", []))
    critical_top = [r.feature for r in results if r.severity == "critical" and r.feature in top_priority]
    drifted = [r for r in results if r.is_drifted]
    share = round(len(drifted) / len(results), 3) if results else 0.0

    share_critical = thresholds["share_of_drifted_features"]["critical"]
    share_warn = thresholds["share_of_drifted_features"]["warn"]
    prediction_critical = thresholds["prediction_drift_ks_p_value"]["critical"]
    prediction_warn = thresholds["prediction_drift_ks_p_value"]["warn"]

    severity = "none"
    if critical_top:
        severity = "critical"
        reasons.append(f"top-importance feature(s) breached threshold: {', '.join(sorted(critical_top))}")
    if share >= share_critical:
        severity = "critical"
        reasons.append(f"{share:.0%} of monitored features drifted (critical at {share_critical:.0%})")
    if prediction_p_value is not None and prediction_p_value < prediction_critical:
        severity = "critical"
        reasons.append(f"prediction distribution shifted (KS p={prediction_p_value:.2g})")

    if severity == "none":
        unseen = [r.feature for r in results if r.detail.get("unseen_categories")]
        if unseen:
            severity = "critical"
            reasons.append(f"unseen category values in: {', '.join(unseen)}")

    if severity == "none" and drifted:
        severity = "warn"
        if share >= share_warn:
            reasons.append(f"{share:.0%} of monitored features drifted (warn at {share_warn:.0%})")
        else:
            reasons.append(f"feature(s) above warn threshold: {', '.join(r.feature for r in drifted)}")
        if prediction_p_value is not None and prediction_p_value < prediction_warn:
            reasons.append(f"prediction distribution mildly shifted (KS p={prediction_p_value:.2g})")

    if severity == "none":
        reasons.append("all monitored features within thresholds")

    return severity, reasons


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------
def render_html_report(report: dict) -> str:
    """Self-contained HTML report (Evidently is used when available, see build_report)."""
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['feature']))}</td>"
        f"<td>{html.escape(str(item['type']))}</td>"
        f"<td>{html.escape(str(item['metric']))}</td>"
        f"<td>{item['score']}</td>"
        f"<td>{item['threshold']}</td>"
        f"<td><span class='badge {item['severity']}'>{item['severity']}</span></td>"
        f"<td>{html.escape(str(item.get('change_summary', '')))}</td>"
        "</tr>"
        for item in report["features"]
    )
    reasons = "".join(f"<li>{html.escape(reason)}</li>" for reason in report["reasons"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChurnOps drift report - {html.escape(report['batch_id'])}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 2rem; background: #0f172a; color: #e2e8f0; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .25rem; }}
  .meta {{ color: #94a3b8; font-size: .85rem; margin-bottom: 1.5rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px;
          padding: .9rem 1.1rem; min-width: 170px; }}
  .card .label {{ color: #94a3b8; font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; }}
  .card .value {{ font-size: 1.5rem; font-weight: 600; margin-top: .25rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #1e293b;
          border-radius: 10px; overflow: hidden; font-size: .9rem; }}
  th, td {{ padding: .7rem .9rem; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #334155; font-weight: 600; }}
  .badge {{ padding: 2px 8px; border-radius: 999px; font-size: .75rem; font-weight: 600; }}
  .critical {{ background: #ef4444; color: #fff; }}
  .warn {{ background: #f59e0b; color: #1f2937; }}
  .none {{ background: #10b981; color: #052e24; }}
  ul {{ color: #cbd5e1; }}
  footer {{ margin-top: 2rem; color: #64748b; font-size: .78rem; }}
</style>
</head>
<body>
  <h1>Drift report · {html.escape(report['batch_id'])}
    <span class="badge {report['severity']}">{report['severity'].upper()}</span></h1>
  <div class="meta">
    batch_date {html.escape(str(report['batch_date']))} ·
    trace_id {html.escape(report['trace_id'])} ·
    reference {html.escape(str(report['reference']['model_version']))}
    ({report['reference']['row_count']} rows, snapshot {html.escape(str(report['reference']['snapshot_date']))})
  </div>

  <div class="cards">
    <div class="card"><div class="label">Drifted features</div>
      <div class="value">{report['share_of_drifted_features'] * 100:.0f}%</div></div>
    <div class="card"><div class="label">Rows scored</div>
      <div class="value">{report['row_count']}</div></div>
    <div class="card"><div class="label">Prediction drift p</div>
      <div class="value">{report['prediction_drift']['p_value']}</div></div>
    <div class="card"><div class="label">Score source</div>
      <div class="value" style="font-size:1rem">{html.escape(report['prediction_drift']['source'])}</div></div>
  </div>

  <h2 style="font-size:1rem">Why this verdict</h2>
  <ul>{reasons}</ul>

  <table>
    <thead><tr><th>Feature</th><th>Type</th><th>Metric</th><th>Score</th>
      <th>Critical at</th><th>Status</th><th>Change</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <footer>Generated by data/drift/detector.py · policy v{html.escape(str(report['policy_version']))}
    · {html.escape(report['created_at'])}</footer>
</body>
</html>
"""


def _try_evidently_report(current: pd.DataFrame, reference: pd.DataFrame, columns: list[str]) -> str | None:
    """Evidently's data-drift preset, when the library is installed in the image."""
    try:  # pragma: no cover - optional dependency
        from evidently.metric_preset import DataDriftPreset
        from evidently.report import Report

        report = Report(metrics=[DataDriftPreset()])
        report.run(reference_data=reference[columns], current_data=current[columns])
        return report.get_html()
    except Exception:
        return None


# ----------------------------------------------------------------------
# main analysis
# ----------------------------------------------------------------------
def run_drift_analysis(
    current_batch_path: str,
    reference_data_path: str | None = None,
    policy_path: str | None = None,
    reports_dir: str | None = None,
    events_dir: str | None = None,
    warehouse_dir: str | None = None,
    publish: bool = True,
    now: datetime.datetime | None = None,
) -> dict:
    policy = load_policy(policy_path)
    contract = load_feature_contract()
    warehouse = Warehouse.default(root=warehouse_dir)
    reports = Storage.for_role("reports", local_root=reports_dir)
    publisher = EventPublisher(outbox_dir=events_dir or EventPublisher.outbox_dir, service="drift-detector")
    now = now or _utc_now()

    current = read_tabular(current_batch_path)
    batch_id = str(current["batch_id"].iloc[0]) if "batch_id" in current.columns else "b-unknown"
    trace_id = str(current["trace_id"].iloc[0]) if "trace_id" in current.columns else "tr-unknown"
    batch_date = (
        str(current["batch_date"].iloc[0]) if "batch_date" in current.columns else datetime.date.today().isoformat()
    )

    window = (
        ReferenceWindow(
            frame=read_tabular(reference_data_path),
            model_version=os.getenv("CHURNOPS_CHAMPION_VERSION", "champion"),
            snapshot_date=batch_date,
            sample_path=reference_data_path,
        )
        if reference_data_path
        else ReferenceWindow.load(warehouse)
    )

    if window is None:
        log.warning(
            "No reference window available; run `make seed` or wait for the first model.promoted",
            trace_id,
            batch_id=batch_id,
        )
        return {"status": "skipped", "reason": "no_reference_window", "batch_id": batch_id, "severity": "none"}

    log.info(
        f"Scoring drift for batch {batch_id} against reference {window.model_version}",
        trace_id,
        batch_id=batch_id,
        metrics={"current_rows": len(current), "reference_rows": window.row_count},
    )

    # ---- feature drift ------------------------------------------------
    monitored = policy.get("monitored_features", {})
    numeric_cols = [c for c in monitored.get("numeric", contract.numeric_features) if c in current.columns and c in window.frame.columns]
    categorical_cols = [
        c for c in monitored.get("categorical", contract.categorical_features)
        if c in current.columns and c in window.frame.columns
    ]
    numeric_thresholds = policy["thresholds"]["numeric_psi"]
    categorical_thresholds = policy["thresholds"]["categorical_jensen_shannon"]
    buckets = int(policy.get("psi_buckets", 10))

    results: list[FeatureDrift] = []
    for column in numeric_cols:
        results.append(
            score_numeric_feature(
                column,
                window.frame[column],
                current[column],
                numeric_thresholds["warn"],
                numeric_thresholds["critical"],
                buckets,
            )
        )
    for column in categorical_cols:
        results.append(
            score_categorical_feature(
                column,
                window.frame[column],
                current[column],
                categorical_thresholds["warn"],
                categorical_thresholds["critical"],
            )
        )

    # ---- prediction drift ---------------------------------------------
    prediction_statistic, prediction_p_value, score_source, scored_model = 0.0, None, "disabled", "unknown"
    if policy.get("prediction_drift", {}).get("enabled", True):
        scores = collect_scores(current, window.frame, policy, batch_id, trace_id, warehouse, contract)
        if scores.available:
            prediction_statistic, prediction_p_value = ks_test(scores.reference, scores.current)
            score_source, scored_model = scores.source, scores.model_version
        else:
            score_source = "unavailable"

    # ---- verdict --------------------------------------------------------
    severity, reasons = decide_severity(results, prediction_p_value, policy, len(current))
    cooldown = cooldown_state(warehouse, policy, severity, now)
    drifted = [r for r in results if r.is_drifted]
    share_drifted = round(len(drifted) / len(results), 3) if results else 0.0

    # One line per feature so P3's log-based metric can label by feature
    # (ops/dashboards/data_quality_metrics.md).
    for item in results:
        log.log(
            "WARNING" if item.is_drifted else "INFO",
            f"Feature drift {item.feature} {item.metric}={item.score}",
            trace_id,
            batch_id=batch_id,
            metrics={
                "feature": item.feature,
                "metric": item.metric,
                "score": item.score,
                "threshold": item.threshold,
                "feature_severity": item.severity,
            },
        )

    feature_rows = []
    for item in sorted(results, key=lambda r: r.score, reverse=True):
        row = item.as_dict()
        if item.type == "numeric":
            row["change_summary"] = (
                f"mean {item.detail['reference_mean']} -> {item.detail['current_mean']} "
                f"({percent_change(item.detail['reference_mean'], item.detail['current_mean']):+.1f}%)"
            )
        else:
            unseen = item.detail.get("unseen_categories") or []
            row["change_summary"] = (
                f"unseen: {', '.join(unseen)}" if unseen
                else f"top {item.detail.get('reference_top')} -> {item.detail.get('current_top')}"
            )
        feature_rows.append(row)

    report = {
        "report_id": f"rep-{batch_id}",
        "batch_id": batch_id,
        "batch_date": batch_date,
        "trace_id": trace_id,
        "severity": severity,
        "reasons": reasons,
        "dataset_drift_detected": severity != "none",
        "share_of_drifted_features": share_drifted,
        "row_count": int(len(current)),
        "policy_version": policy.get("version", "unknown"),
        "contract_version": contract.version,
        "reference": {
            "model_version": window.model_version,
            "snapshot_date": window.snapshot_date,
            "row_count": window.row_count,
        },
        "prediction_drift": {
            "ks_statistic": prediction_statistic,
            "p_value": prediction_p_value,
            "source": score_source,
            "scored_model_version": scored_model,
        },
        "cooldown": cooldown,
        "features": feature_rows,
        "created_at": now.isoformat(),
    }

    # ---- persist report --------------------------------------------------
    json_path = reports.write_json(f"drift/{batch_id}/report.json", report)
    html_body = None
    if policy.get("report", {}).get("evidently", False):
        html_body = _try_evidently_report(current, window.frame, numeric_cols + categorical_cols)
    html_path = reports.write_text(f"drift/{batch_id}/report.html", html_body or render_html_report(report), "text/html")
    json_uri = reports.uri(f"drift/{batch_id}/report.json")
    html_uri = reports.uri(f"drift/{batch_id}/report.html")

    # ---- publish ---------------------------------------------------------
    log.info(
        f"Drift verdict for batch {batch_id}: {severity}",
        trace_id,
        batch_id=batch_id,
        metrics={
            "verdict_severity": severity,
            "share_of_drifted_features": share_drifted,
            "drifted_features": len(drifted),
            "reasons": reasons,
        },
    )

    event_payload = None
    should_publish = severity == policy.get("auto_trigger_retraining_severity", "critical") and not cooldown["suppressed"]
    if should_publish and publish:
        event_payload = build_event(
            "drift.detected",
            trace_id=trace_id,
            batch_id=batch_id,
            batch_date=batch_date,
            severity=severity,
            share_of_drifted_features=share_drifted,
            drifted_features=[item.as_event_item() for item in sorted(drifted, key=lambda r: r.score, reverse=True)],
            prediction_drift_p_value=prediction_p_value,
            report_uri=html_uri,
        )
        publisher.publish("drift.detected", event_payload, trace_id=trace_id)
        log.warning(
            f"CRITICAL drift in batch {batch_id}: {reasons[0]}",
            trace_id,
            batch_id=batch_id,
            metrics={
                "share_of_drifted_features": share_drifted,
                "top_features": [item.feature for item in drifted[:5]],
                "prediction_drift_p_value": prediction_p_value,
            },
            report_uri=html_uri,
            alert="drift_detected",
        )
    elif cooldown["suppressed"]:
        log.warning(
            f"Critical drift in batch {batch_id} suppressed by the {cooldown['cooldown_hours']}h cooldown",
            trace_id,
            batch_id=batch_id,
            metrics=cooldown,
        )

    # ---- warehouse summary row -------------------------------------------
    warehouse.insert_row(
        "drift_reports",
        {
            "report_id": report["report_id"],
            "batch_id": batch_id,
            "batch_date": batch_date,
            "trace_id": trace_id,
            "severity": severity,
            "dataset_drift_detected": severity != "none",
            "share_of_drifted_features": share_drifted,
            "top_drifted_features": json.dumps([item.as_event_item() for item in drifted[:5]]),
            "prediction_drift_p_value": prediction_p_value,
            "gcs_report_html_uri": html_uri,
            "gcs_report_json_uri": json_uri,
            "realised_auc": None,
            "created_at": report["created_at"],
            "published_event": bool(event_payload),
        },
    )

    return {
        "status": "completed",
        "batch_id": batch_id,
        "trace_id": trace_id,
        "severity": severity,
        "reasons": reasons,
        "share_drifted": share_drifted,
        "cooldown": cooldown,
        "report": report,
        "report_json": json_path,
        "report_html": html_path,
        "report_uri": html_uri,
        "event_payload": event_payload,
    }


def handle_model_promoted(event: dict, warehouse_dir: str | None = None) -> dict:
    """model.promoted -> the new champion's training data becomes the reference window."""
    return refresh_on_model_promoted(event, Warehouse.default(root=warehouse_dir))


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="ChurnOps drift detector")
    parser.add_argument("batch_file", nargs="?", help="Current batch (path or gs:// URI)")
    parser.add_argument("--reference-file", default=None, help="Override the frozen reference window")
    parser.add_argument("--policy-file", default=None)
    parser.add_argument("--reports-dir", default=None)
    parser.add_argument("--events-dir", default=None)
    parser.add_argument("--warehouse-dir", default=None)
    parser.add_argument("--no-publish", action="store_true", help="Score and report without emitting drift.detected")
    parser.add_argument("--refresh-reference", metavar="MODEL_VERSION", help="Rebuild the reference window and exit")
    args = parser.parse_args(argv)

    if args.refresh_reference:
        result = handle_model_promoted(
            {"promoted_model_version": args.refresh_reference, "trace_id": "tr-manual-refresh"},
            warehouse_dir=args.warehouse_dir,
        )
        print(json.dumps(result, indent=2, default=str))
        return result

    if not args.batch_file:
        parser.error("Provide a batch file or --refresh-reference")

    result = run_drift_analysis(
        current_batch_path=args.batch_file,
        reference_data_path=args.reference_file,
        policy_path=args.policy_file,
        reports_dir=args.reports_dir,
        events_dir=args.events_dir,
        warehouse_dir=args.warehouse_dir,
        publish=not args.no_publish,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "report"}, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
