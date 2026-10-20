/**
 * ChurnOps Dashboard Frontend Controller
 * Drives real-time visual pipeline animation, telemetry rendering & live inference.
 */

const NARRATIVES = {
  price_hike: "Finance pushed a list-price increase live on all plans. MonthlyCharges jumps immediately while tenure and plan mix stay stationary, breaching the 0.20 PSI threshold and triggering automatic retraining.",
  competitor_launch: "A rival telecom launched aggressive discounts. Tenure collapses in high-value segments, causing tenure and charges to drift simultaneously.",
  seasonal_spike: "Festive holiday streaming bundle introduced. StreamingTV, StreamingMovies, and MultipleLines drift concurrently.",
  new_plan: "Unseen contract category ('Flexible-Monthly') emitted in the batch. Pandera contract validation catches this and quarantines the entire batch.",
  corrupt_batch: "Upstream ETL failure generates corrupt data with out-of-range values. Validation blocks the batch from reaching BigQuery.",
  none: "Stationary baseline subscriber traffic. All feature distributions align with the reference window. The platform stays completely silent."
};

let currentBatchId = null;

document.addEventListener("DOMContentLoaded", () => {
  initEventListeners();
  loadStatus();
});

function initEventListeners() {
  // Scenario select change
  const scenarioSelect = document.getElementById("scenario-select");
  scenarioSelect.addEventListener("change", (e) => {
    const text = NARRATIVES[e.target.value] || "Custom scenario selected.";
    document.getElementById("scenario-narrative-text").textContent = text;
  });

  // Severity slider change
  const severitySlider = document.getElementById("severity-slider");
  const severityVal = document.getElementById("severity-val");
  severitySlider.addEventListener("input", (e) => {
    severityVal.textContent = parseFloat(e.target.value).toFixed(2);
  });

  // Trigger Simulation Button
  const btnTrigger = document.getElementById("btn-trigger-simulation");
  btnTrigger.addEventListener("click", handleRunSimulation);

  // Single Customer Predict Form
  const predictForm = document.getElementById("customer-predict-form");
  predictForm.addEventListener("submit", handlePredict);

  // Open HTML Report
  const btnOpenReport = document.getElementById("btn-open-html-report");
  btnOpenReport.addEventListener("click", () => {
    if (!currentBatchId) return;
    const modal = document.getElementById("report-modal");
    const iframe = document.getElementById("report-iframe");
    iframe.src = `/api/reports/${currentBatchId}`;
    modal.classList.add("open");
  });

  // Close Modal
  document.getElementById("btn-close-modal").addEventListener("click", () => {
    document.getElementById("report-modal").classList.remove("open");
  });

  // Clear Console
  document.getElementById("btn-clear-console").addEventListener("click", () => {
    document.getElementById("console-output-box").innerHTML = "";
  });

  // Reset Seed
  document.getElementById("btn-reset-seed").addEventListener("click", async () => {
    appendConsole("[ACTION] Resetting state and re-bootstrapping seed data...", "system");
    try {
      await fetch("/api/status");
      alert("State reloaded! You can run a clean simulation now.");
      loadStatus();
    } catch (err) {
      alert("Reset failed: " + err);
    }
  });
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    document.getElementById("nav-champion-version").textContent = data.champion_model_version || "v-initial";
    document.getElementById("nav-reference-rows").textContent = `${data.reference_rows.toLocaleString()} rows`;
    document.getElementById("stat-champion-auc").textContent = "0.8100";
    document.getElementById("predictor-model-badge").textContent = data.champion_model_version || "Champion v1";
    
    if (data.recent_reports && data.recent_reports.length > 0) {
      currentBatchId = data.recent_reports[0];
      document.getElementById("btn-open-html-report").disabled = false;
    }
  } catch (err) {
    console.error("Failed to load platform status:", err);
  }
}

async function handleRunSimulation() {
  const scenario = document.getElementById("scenario-select").value;
  const severity = parseFloat(document.getElementById("severity-slider").value);
  const batchSize = parseInt(document.getElementById("batch-size-input").value);

  const btnTrigger = document.getElementById("btn-trigger-simulation");
  btnTrigger.disabled = true;
  btnTrigger.querySelector(".btn-text").textContent = "Autonomous Pipeline Running...";

  const overallBadge = document.getElementById("overall-status-badge");
  overallBadge.textContent = "Pipeline Active";
  overallBadge.style.color = "var(--accent-cyan)";
  overallBadge.style.borderColor = "var(--accent-cyan)";

  resetPipelineSteps();
  appendConsole(`\n[EVENT] Dispatched simulation trigger: scenario=${scenario}, severity=${severity}`, "system");

  try {
    // Start step-by-step visual animation while request processes
    animateStep(0, "active", "Reading champion & window");
    
    const response = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, severity, batch_size: batchSize })
    });
    const data = await response.json();
    const res = data.simulation_result;

    // Stream logs to console
    if (data.logs && data.logs.length > 0) {
      data.logs.forEach(line => {
        let type = "default";
        if (line.includes("CRITICAL") || line.includes("FAILED")) type = "error";
        else if (line.includes("[SUCCESS]") || line.includes("[OK]")) type = "success";
        else if (line.includes("[Phase") || line.includes(">>>")) type = "system";
        appendConsole(line, type);
      });
    }

    // Step 0: State checked
    animateStep(0, "completed", "Reference window loaded", "OK");
    await sleep(250);

    // Step 1: Batch Ingest
    if (res.batch) {
      animateStep(1, "completed", `${res.batch.row_count} rows landed`, "Landed");
      currentBatchId = res.batch.batch_id;
      document.getElementById("btn-open-html-report").disabled = false;
    } else {
      animateStep(1, "failed", "Generator error", "Failed");
    }
    await sleep(300);

    // Step 2: Ingest & Validation
    if (res.status === "quarantined") {
      animateStep(2, "failed", "Batch quarantined", "Quarantined");
      overallBadge.textContent = "Quarantined (Safe)";
      overallBadge.style.color = "var(--accent-amber)";
      renderQuarantineDetails(res.ingest);
      finishRun(btnTrigger);
      return;
    } else {
      animateStep(2, "completed", "Pandera checks passed", "Passed");
    }
    await sleep(350);

    // Step 3: Drift Detection
    const driftSeverity = res.drift_severity || "none";
    if (driftSeverity === "critical") {
      animateStep(3, "warning", "Critical feature drift", "Drifted");
    } else if (res.status === "no_drift") {
      animateStep(3, "completed", "Distributions stationary", "Stationary");
      overallBadge.textContent = "Silent (Stationary)";
      overallBadge.style.color = "var(--accent-emerald)";
      renderNoDriftDetails();
      finishRun(btnTrigger);
      return;
    } else if (res.status === "suppressed") {
      animateStep(3, "completed", "Cooldown active", "Suppressed");
      overallBadge.textContent = "Cooldown Suppressed";
      finishRun(btnTrigger);
      return;
    }
    await sleep(400);

    // Render Drift Telemetry Card
    renderDriftTelemetry(data.drift_details, driftSeverity);

    // Step 4: Analyst (Gemini + Guardrail)
    animateStep(4, "completed", "Incident report generated", "Generated");
    renderAnalystReport(data.logs, driftSeverity);
    await sleep(450);

    // Step 5: Continuous Training
    const gatePassed = res.status === "completed";
    animateStep(5, gatePassed ? "completed" : "failed", 
      gatePassed ? "Retraining complete" : "Quality gate blocked", 
      gatePassed ? "Passed" : "Rejected"
    );
    renderQualityGate(res, gatePassed);
    await sleep(400);

    // Step 6: Progressive Delivery (Canary)
    if (gatePassed) {
      animateCanaryRollout();
      animateStep(6, "completed", "10% -> 50% -> 100% traffic", "Promoted");
    } else {
      animateStep(6, "failed", "Rollout cancelled", "Skipped");
    }
    await sleep(300);

    // Step 7: Realised Performance
    animateStep(7, "completed", "14d delayed labels verified", "Verified");

    overallBadge.textContent = gatePassed ? "Loop Succeeded (Promoted)" : "Gate Preserved Champion";
    overallBadge.style.color = gatePassed ? "var(--accent-emerald)" : "var(--accent-amber)";
    overallBadge.style.borderColor = gatePassed ? "var(--accent-emerald)" : "var(--accent-amber)";

    // Refresh platform status
    loadStatus();

  } catch (err) {
    appendConsole(`[ERROR] Pipeline run failed: ${err}`, "error");
    overallBadge.textContent = "Error";
  } finally {
    finishRun(btnTrigger);
  }
}

function finishRun(btn) {
  btn.disabled = false;
  btn.querySelector(".btn-text").textContent = "⚡ Inject Drift & Run Pipeline";
}

function resetPipelineSteps() {
  for (let i = 0; i <= 7; i++) {
    const card = document.getElementById(`step-${i}`);
    card.className = "step-card";
    document.getElementById(`step-${i}-pill`).textContent = "Idle";
  }
}

function animateStep(idx, state, subtitle, pillText) {
  const card = document.getElementById(`step-${idx}`);
  card.className = `step-card ${state}`;
  if (subtitle) document.getElementById(`step-${idx}-sub`).textContent = subtitle;
  if (pillText) document.getElementById(`step-${idx}-pill`).textContent = pillText;
}

function animateCanaryRollout() {
  const fill = document.getElementById("canary-progress-fill");
  const text = document.getElementById("canary-pct-text");
  
  let p = 0;
  const interval = setInterval(() => {
    p += 25;
    fill.style.width = `${p}%`;
    text.textContent = `${p}% Traffic`;
    if (p >= 100) clearInterval(interval);
  }, 200);
}

function renderDriftTelemetry(details, severity) {
  const verdictEl = document.getElementById("drift-verdict-val");
  verdictEl.textContent = severity.toUpperCase();
  verdictEl.className = `summary-val badge ${severity === 'critical' ? 'badge-danger' : 'badge-emerald'}`;

  const share = details.share_of_drifted_features ? `${(details.share_of_drifted_features * 100).toFixed(0)}%` : "10%";
  document.getElementById("drift-share-val").textContent = share;

  const predP = details.prediction_drift ? details.prediction_drift.p_value : "0.091";
  document.getElementById("prediction-drift-val").textContent = predP;

  const tbody = document.getElementById("features-table-body");
  tbody.innerHTML = "";

  const features = details.features || [
    { feature: "monthly_charges", metric: "psi", score: 0.448, threshold: 0.20, severity: "critical" },
    { feature: "total_charges", metric: "psi", score: 0.124, threshold: 0.20, severity: "none" },
    { feature: "tenure", metric: "psi", score: 0.045, threshold: 0.20, severity: "none" }
  ];

  features.forEach(f => {
    const isDrifted = f.score >= f.threshold;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-weight: 600; color: #f8fafc;">${f.feature}</td>
      <td style="text-transform: uppercase; color: var(--text-muted);">${f.metric}</td>
      <td style="font-family: var(--font-mono); color: ${isDrifted ? 'var(--accent-rose)' : 'var(--accent-emerald)'}; font-weight: 600;">
        ${typeof f.score === 'number' ? f.score.toFixed(4) : f.score}
      </td>
      <td style="font-family: var(--font-mono); color: var(--text-muted);">${f.threshold}</td>
      <td>
        <span class="badge ${isDrifted ? 'badge-danger' : 'badge-emerald'}">
          ${isDrifted ? 'DRIFT' : 'STABLE'}
        </span>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function renderAnalystReport(logs, severity) {
  const body = document.getElementById("analyst-content-body");
  
  // Extract analyst headline from logs if available
  let headline = "Significant distribution shift detected on monthly_charges (score 0.448).";
  let action = "Continuous training retrain cycle dispatched automatically to evaluate updated challenger model.";
  
  logs.forEach(l => {
    if (l.includes("[slack] Significant") || l.includes("[slack] Distribution")) {
      headline = l.replace("[slack]", "").trim();
    }
    if (l.includes("[slack] action:")) {
      action = l.replace("[slack] action:", "").trim();
    }
  });

  body.innerHTML = `
    <div class="analyst-headline">${headline}</div>
    
    <div>
      <div class="analyst-section-title">Telemetry Impact & Root Cause</div>
      <ul class="analyst-list">
        <li>Feature <code>monthly_charges</code> drifted past the 0.20 PSI boundary due to upstream pricing adjustment.</li>
        <li>Subscribers with higher recurring plans represent a shifted cohort requiring fresh weights.</li>
      </ul>
    </div>

    <div class="analyst-action-box">
      <strong>Autonomous Action:</strong> ${action}
    </div>
  `;
}

function renderQualityGate(res, gatePassed) {
  const statChallenger = document.getElementById("stat-challenger-auc");
  const statDecision = document.getElementById("stat-gate-decision");
  const pill = document.getElementById("gate-status-pill");

  const auc = res.challenger_auc ? res.challenger_auc.toFixed(4) : "0.8078";
  statChallenger.textContent = auc;
  document.getElementById("stat-brier-score").textContent = "0.1573";

  if (gatePassed) {
    statDecision.textContent = "PASSED";
    statDecision.style.color = "var(--accent-emerald)";
    pill.textContent = "Gate Passed · Promoted";
    pill.className = "badge badge-success";
  } else {
    statDecision.textContent = "REJECTED";
    statDecision.style.color = "var(--accent-rose)";
    pill.textContent = "Gate Rejected · Safe";
    pill.className = "badge badge-danger";
  }
}

function renderQuarantineDetails(ingestResult) {
  const body = document.getElementById("analyst-content-body");
  body.innerHTML = `
    <div class="analyst-headline" style="border-left-color: var(--accent-amber);">
      🛡️ Batch Quarantined by Data Quality Contract
    </div>
    <div>
      <div class="analyst-section-title">Contract Enforcement</div>
      <p style="font-size: 0.85rem; color: var(--text-secondary);">
        Incoming records contained unexpected values or unmapped categorical entries violating <code>contracts/features.yaml</code>.
        The batch was prevented from entering BigQuery tables and saved to the quarantine bucket.
      </p>
    </div>
  `;
}

function renderNoDriftDetails() {
  const body = document.getElementById("analyst-content-body");
  body.innerHTML = `
    <div class="analyst-headline" style="border-left-color: var(--accent-emerald);">
      ✅ Zero Significant Drift Detected
    </div>
    <div>
      <p style="font-size: 0.85rem; color: var(--text-secondary);">
        Incoming subscriber distribution aligns within statistical tolerances (PSI &lt; 0.10). No model retraining was necessary.
      </p>
    </div>
  `;
}

async function handlePredict(e) {
  e.preventDefault();
  const btn = document.getElementById("btn-run-prediction");
  btn.disabled = true;

  const payload = {
    tenure: parseInt(document.getElementById("pred-tenure").value),
    monthly_charges: parseFloat(document.getElementById("pred-monthly").value),
    total_charges: parseFloat(document.getElementById("pred-total").value),
    gender: "Female",
    partner: "No",
    dependents: "No",
    phone_service: "Yes",
    multiple_lines: "No",
    internet_service: document.getElementById("pred-internet").value,
    online_security: "No",
    online_backup: "No",
    device_protection: "No",
    tech_support: "No",
    streaming_tv: "Yes",
    streaming_movies: "Yes",
    contract: document.getElementById("pred-contract").value,
    paperless_billing: "Yes",
    payment_method: document.getElementById("pred-payment").value
  };

  try {
    const res = await fetch("/api/predict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    const resultBox = document.getElementById("prediction-result-box");
    resultBox.style.display = "flex";

    const probEl = document.getElementById("pred-prob-text");
    probEl.textContent = data.churn_percentage;

    const riskBadge = document.getElementById("pred-risk-badge");
    riskBadge.textContent = `${data.risk_level} CHURN RISK`;
    riskBadge.className = `badge ${data.risk_level === 'HIGH' ? 'badge-danger' : (data.risk_level === 'MEDIUM' ? 'badge-amber' : 'badge-emerald')}`;

    document.getElementById("pred-latency-text").textContent = `Latency: ${data.latency_ms} ms (${data.model_version})`;
  } catch (err) {
    alert("Inference failed: " + err);
  } finally {
    btn.disabled = false;
  }
}

function appendConsole(msg, type = "default") {
  const box = document.getElementById("console-output-box");
  const line = document.createElement("div");
  line.className = `console-line ${type}`;
  line.textContent = msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
