"""Turn the run JSONL into a summary table and (optionally) the figures.

Always writes results/summary.csv and prints a readable table using only the
standard library. If matplotlib is installed, it also writes the three figures
from the plan:

  Fig 1  target hit-rate vs hint level, one line per model (the headline)
  Fig 2  mean false positives vs hint level per model (the noise cost)
  Fig 3  effort (tool calls) vs hint level per model

No API calls. Reads a JSONL produced by harness.run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from typing import Any

LEVEL_ORDER = ["L0", "L1", "L2", "L3"]


def _read(jsonl_path: str) -> list[dict[str, Any]]:
    rows = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _scored(records: list[dict], field: str) -> list[dict]:
    """Records where the target was actually scored (drop refusals)."""
    return [r for r in records if r.get(field) != "REFUSED"]


def _rate(records: list[dict], field: str, value: str) -> float:
    scored = _scored(records, field)
    if not scored:
        return 0.0
    return sum(1 for r in scored if r.get(field) == value) / len(scored)


def _mean(records: list[dict], getter) -> float:
    vals = [getter(r) for r in records if getter(r) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["level"])].append(r)

    out = []
    for (model, level), recs in groups.items():
        n_refused = sum(1 for r in recs if r.get("t618") == "REFUSED")
        all_refused = n_refused == len(recs) and len(recs) > 0
        rate = (lambda f, v: None) if all_refused else (
            lambda f, v: round(_rate(recs, f, v), 3))
        out.append({
            "model": model,
            "level": level,
            "n": len(recs),
            "n_refused": n_refused,
            "hit_t618": rate("t618", "HIT"),
            "hit_t619": rate("t619", "HIT"),
            "partial_t618": rate("t618", "PARTIAL"),
            "partial_t619": rate("t619", "PARTIAL"),
            "mean_fp": round(_mean(recs, lambda r: r.get("n_false_positives", 0)), 2),
            "mean_tool_calls": round(_mean(recs, lambda r: r.get("effort", {}).get("tool_calls")), 1),
            "est_usd": round(sum(r.get("est_usd", 0) for r in recs), 3),
        })
    out.sort(key=lambda d: (d["model"], LEVEL_ORDER.index(d["level"])
                            if d["level"] in LEVEL_ORDER else 99))
    return out


def write_csv(summary: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["model", "level", "n", "n_refused", "hit_t618", "hit_t619",
              "partial_t618", "partial_t619", "mean_fp", "mean_tool_calls", "est_usd"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(summary)


def print_table(summary: list[dict]) -> None:
    cols = ["model", "level", "n", "n_refused", "hit_t618", "hit_t619", "mean_fp", "mean_tool_calls", "est_usd"]

    def cell(row, c):
        v = row[c]
        return "n/a" if v is None else str(v)

    widths = {c: max(len(c), *(len(cell(row, c)) for row in summary)) if summary else len(c)
              for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for row in summary:
        print("  ".join(cell(row, c).ljust(widths[c]) for c in cols))


def make_figures(summary: list[dict], results_dir: str) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not installed — skipping figures; summary.csv is written)")
        return []

    models = sorted({r["model"] for r in summary})

    def series(model, field):
        bymodel = {r["level"]: r for r in summary if r["model"] == model}
        pairs = [(lvl, bymodel[lvl][field]) for lvl in LEVEL_ORDER
                 if lvl in bymodel and bymodel[lvl][field] is not None]
        return [p[0] for p in pairs], [p[1] for p in pairs]

    written = []
    specs = [
        ("fig1_hit_rate.png", "Target hit-rate vs hint level",
         [("hit_t618", "t618"), ("hit_t619", "t619")], "hit-rate"),
        ("fig2_false_positives.png", "Mean false positives vs hint level",
         [("mean_fp", "mean FP")], "mean false positives"),
        ("fig3_effort.png", "Effort (tool calls) vs hint level",
         [("mean_tool_calls", "tool calls")], "mean tool calls"),
    ]
    for fname, title, fields, ylabel in specs:
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for model in models:
            for field, flabel in fields:
                xs, ys = series(model, field)
                label = f"{model} {flabel}" if len(fields) > 1 else model
                ax.plot(xs, ys, marker="o", label=label)
        ax.set_title(title)
        ax.set_xlabel("hint level")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        path = os.path.join(results_dir, fname)
        fig.savefig(path, dpi=120)
        plt.close(fig)
        written.append(path)
    return written


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description="Summarize a PHP2Shell run JSONL.")
    ap.add_argument("--runs", required=True, help="path to data/runs/<timestamp>.jsonl")
    ap.add_argument("--results-dir", default="data/results")
    args = ap.parse_args(argv)

    rows = _read(args.runs)
    if not rows:
        print("No records found.")
        return
    summary = summarize(rows)
    os.makedirs(args.results_dir, exist_ok=True)
    csv_path = os.path.join(args.results_dir, "summary.csv")
    write_csv(summary, csv_path)
    print_table(summary)
    print(f"\nWrote {csv_path}")
    figs = make_figures(summary, args.results_dir)
    for f in figs:
        print(f"Wrote {f}")


if __name__ == "__main__":
    main()
