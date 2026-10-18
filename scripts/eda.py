"""
Exploratory data analysis on the seed dataset (Person 1, Phase A).

`make eda` writes plots and a written summary to docs/eda/. This is the evidence
behind contracts/features.yaml: which columns are skewed, where TotalCharges is
blank, how imbalanced the label is, and which segments churn. It runs headless in
CI (Agg backend) and re-runs idempotently.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "data", "generator"))

from common.contracts import load_feature_contract  # noqa: E402
from common.logging_utils import get_logger  # noqa: E402

log = get_logger("eda")

OUTPUT_DIR = os.path.join("docs", "eda")
PALETTE = {"bar": "#2563eb", "alt": "#f59e0b", "grid": "#e2e8f0", "text": "#0f172a"}


def _style(ax, title: str, xlabel: str = "", ylabel: str = "") -> None:
    ax.set_title(title, fontsize=11, color=PALETTE["text"], pad=10)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(axis="y", color=PALETTE["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _save(fig, name: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, name)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_numeric_distributions(df: pd.DataFrame, columns: list[str], output_dir: str) -> str:
    fig, axes = plt.subplots(1, len(columns), figsize=(4.2 * len(columns), 3.4))
    axes = np.atleast_1d(axes)
    for ax, column in zip(axes, columns, strict=False):
        series = pd.to_numeric(df[column], errors="coerce").dropna()
        ax.hist(series, bins=30, color=PALETTE["bar"], alpha=0.85)
        _style(ax, f"{column}  (median {series.median():.1f})", column, "customers")
    return _save(fig, "numeric_distributions.png", output_dir)


def plot_class_balance(df: pd.DataFrame, output_dir: str) -> tuple[str, float]:
    counts = pd.to_numeric(df["churn"], errors="coerce").fillna(0).astype(int).value_counts().sort_index()
    rate = float(counts.get(1, 0) / counts.sum())
    fig, ax = plt.subplots(figsize=(4.2, 3.4))
    ax.bar(["retained", "churned"], [counts.get(0, 0), counts.get(1, 0)], color=[PALETTE["bar"], PALETTE["alt"]])
    _style(ax, f"Class balance ({rate:.1%} churn)", "", "customers")
    return _save(fig, "class_balance.png", output_dir), rate


def plot_churn_by_category(df: pd.DataFrame, column: str, output_dir: str) -> str:
    rates = (
        df.assign(churn=pd.to_numeric(df["churn"], errors="coerce"))
        .groupby(column)["churn"]
        .mean()
        .sort_values(ascending=False)
    )
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    ax.bar(rates.index.astype(str), rates.to_numpy(), color=PALETTE["bar"])
    ax.tick_params(axis="x", labelrotation=20, labelsize=8)
    _style(ax, f"Churn rate by {column}", "", "churn rate")
    return _save(fig, f"churn_by_{column}.png", output_dir)


def plot_tenure_vs_contract(df: pd.DataFrame, output_dir: str) -> str:
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    levels = sorted(df["contract"].astype(str).unique())
    ax.boxplot(
        [pd.to_numeric(df.loc[df["contract"] == level, "tenure"], errors="coerce").dropna() for level in levels],
        labels=levels,
        patch_artist=True,
        boxprops={"facecolor": "#bfdbfe", "edgecolor": PALETTE["bar"]},
        medianprops={"color": PALETTE["alt"], "linewidth": 2},
    )
    _style(ax, "Tenure by contract type (the correlation the generator reproduces)", "", "tenure (months)")
    return _save(fig, "tenure_by_contract.png", output_dir)


def plot_missingness(df: pd.DataFrame, output_dir: str) -> tuple[str, dict]:
    missing = (df.isna().mean() * 100).sort_values(ascending=False).head(12)
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.barh(missing.index.astype(str)[::-1], missing.to_numpy()[::-1], color=PALETTE["alt"])
    _style(ax, "Missing values by column (%)", "% missing", "")
    ax.grid(axis="x", color=PALETTE["grid"], linewidth=0.8)
    path = _save(fig, "missingness.png", output_dir)
    return path, {k: round(float(v), 3) for k, v in missing.items() if v > 0}


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as GitHub markdown without pulling in tabulate."""
    header = "| statistic | " + " | ".join(str(c) for c in frame.columns) + " |"
    divider = "| --- " * (len(frame.columns) + 1) + "|"
    rows = [
        "| " + " | ".join([str(index)] + [f"{value}" for value in row]) + " |"
        for index, row in zip(frame.index, frame.to_numpy(), strict=True)
    ]
    return "\n".join([header, divider, *rows])


def summarise(df: pd.DataFrame, churn_rate: float, missing: dict, source: str, plots: list[str]) -> str:
    contract = load_feature_contract()
    numeric = contract.numeric_features
    describe = df[numeric].apply(pd.to_numeric, errors="coerce").describe().round(2)

    lines = [
        "# ChurnOps EDA - seed dataset",
        "",
        f"_Generated by `scripts/eda.py` on {datetime.date.today().isoformat()} from `{source}` "
        f"({len(df):,} rows)._",
        "",
        "## What this tells the platform",
        "",
        f"- **Class balance:** {churn_rate:.1%} of customers churn, so training uses class weights and the "
        "business metric is recall at 70% precision, not accuracy.",
        "- **TotalCharges blanks:** brand-new customers have no billed total; the contract imputes the median "
        "and the generator reproduces the same pattern.",
        "- **Skew:** `total_charges` is heavily right-skewed, which is why `contracts/features.yaml` applies "
        "`log1p` to it and `standard_scale` to `monthly_charges`.",
        "- **Correlation:** tenure rises sharply with contract length; the synthetic generator samples tenure "
        "conditioned on contract so drift scenarios stay believable.",
        "",
        "## Numeric summary",
        "",
        _markdown_table(describe),
        "",
        "## Missing values",
        "",
    ]
    if missing:
        lines += [f"- `{column}`: {pct}% missing" for column, pct in missing.items()]
    else:
        lines.append("- No missing values in this extract.")

    lines += [
        "",
        "## Plots",
        "",
        *[f"![{os.path.basename(p)}]({os.path.basename(p)})" for p in plots],
        "",
        "## Contract implications",
        "",
        "| Observation | contracts/features.yaml decision |",
        "| --- | --- |",
        "| `total_charges` right-skewed, blank for new customers | `transform: log1p`, `impute: median` |",
        "| `monthly_charges` roughly tri-modal by internet service | `transform: standard_scale`, drift watched with PSI |",
        "| `tenure` bounded 0-72 in the extract, 0-100 allowed | `min: 0, max: 100`, `min_max_scale` |",
        "| Service columns carry 'No internet service' | kept as categories, not nulls |",
        "| ~26% churn | `recall_at_70_precision` as the business gate |",
        "",
    ]
    return "\n".join(lines)


def run_eda(seed_file: str, rows: int, output_dir: str = OUTPUT_DIR) -> dict:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
    from bootstrap_seed import load_or_synthesise_seed  # noqa: E402

    df, source = load_or_synthesise_seed(seed_file, rows, datetime.date.today().isoformat())
    contract = load_feature_contract()

    plots = [plot_numeric_distributions(df, contract.numeric_features, output_dir)]
    class_plot, churn_rate = plot_class_balance(df, output_dir)
    plots.append(class_plot)
    plots.append(plot_churn_by_category(df, "contract", output_dir))
    plots.append(plot_churn_by_category(df, "internet_service", output_dir))
    plots.append(plot_tenure_vs_contract(df, output_dir))
    missing_plot, missing = plot_missingness(df, output_dir)
    plots.append(missing_plot)

    summary_path = os.path.join(output_dir, "README.md")
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write(summarise(df, churn_rate, missing, source, plots))

    log.info(
        f"EDA written to {output_dir}",
        "tr-eda",
        metrics={"rows": len(df), "churn_rate": round(churn_rate, 4), "plots": len(plots)},
    )
    return {"rows": len(df), "churn_rate": round(churn_rate, 4), "plots": plots, "summary": summary_path}


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="ChurnOps seed-dataset EDA")
    parser.add_argument("--seed-file", default=os.path.join("data", "seed", "telco_churn_seed.csv"))
    parser.add_argument("--rows", type=int, default=7000)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    result = run_eda(args.seed_file, args.rows, args.output_dir)
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
