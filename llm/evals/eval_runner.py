"""
ChurnOps LLMOps Prompt Evaluation Runner (Person 2)
Evaluates prompt performance against golden benchmark scenarios.
Scores faithfulness, top-feature coverage, and actionability in CI/CD.
"""

import json
import os
import sys

# Ensure parent analyst path is importable
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "analyst"))
from guardrails import DriftIncidentReport, verify_numeric_guardrail
from service import build_context, call_gemini_or_mock
import yaml


def run_eval_suite(
    prompt_path: str = "llm/prompts/drift_analyst/v1.yaml",
    golden_path: str = "llm/evals/golden/scenarios.json",
    min_pass_rate: float = 0.85,
) -> dict:
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_cfg = yaml.safe_load(f)

    with open(golden_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    print(f"\nEvaluating prompt: {prompt_cfg.get('version')} ({prompt_cfg.get('model')}) on {len(scenarios)} golden scenarios...")

    results = []
    passed_cases = 0

    for scen in scenarios:
        scen_id = scen["scenario_id"]
        payload = scen["event_payload"]
        context_str = build_context(payload)

        raw_output = call_gemini_or_mock(prompt_cfg, context_str)
        report = DriftIncidentReport(**raw_output)

        # 1. Faithfulness test
        faith_passed, unanchored = verify_numeric_guardrail(report, context_str)

        # 2. Coverage test
        report_text = f"{report.headline} {' '.join(report.what_changed)}".lower()
        covered_features = [f for f in scen.get("expected_top_features", []) if f.lower() in report_text]
        coverage_score = 1.0 if not scen.get("expected_top_features") else len(covered_features) / len(scen["expected_top_features"])

        # 3. Actionability check
        actionable = len(report.recommended_checks) >= 1 and len(report.model_action) > 10

        case_passed = faith_passed and coverage_score >= 0.8 and actionable
        if case_passed:
            passed_cases += 1

        results.append({
            "scenario_id": scen_id,
            "faithfulness_passed": faith_passed,
            "unanchored_numbers": unanchored,
            "coverage_score": coverage_score,
            "actionable": actionable,
            "case_passed": case_passed,
        })

    overall_pass_rate = passed_cases / len(scenarios) if scenarios else 0.0
    suite_passed = overall_pass_rate >= min_pass_rate

    summary = {
        "prompt_version": prompt_cfg.get("version"),
        "total_scenarios": len(scenarios),
        "passed_cases": passed_cases,
        "pass_rate": round(overall_pass_rate, 3),
        "target_pass_rate": min_pass_rate,
        "suite_passed": suite_passed,
        "results": results,
    }

    print("\n" + "=" * 60)
    print(f"EVALUATION RESULT: {'PASSED' if suite_passed else 'FAILED'}")
    print(f"Pass Rate: {overall_pass_rate * 100:.1f}% (Required: {min_pass_rate * 100:.1f}%)")
    print("=" * 60)
    for r in results:
        status_icon = "PASS" if r["case_passed"] else "FAIL"
        print(f"[{status_icon}] {r['scenario_id']}: Faithfulness={r['faithfulness_passed']}, Coverage={r['coverage_score']*100:.0f}%")
    print("=" * 60 + "\n")

    return summary


if __name__ == "__main__":
    summary = run_eval_suite()
    if not summary["suite_passed"]:
        sys.exit(1)
