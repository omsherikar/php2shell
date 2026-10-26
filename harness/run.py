"""Orchestrate the experiment matrix: agent trials + scoring -> JSONL.

For each enabled model it optionally runs the recall probe (dropping any model
that can recite the fix), then iterates hint levels x trials. Each trial runs the
read-only agent, scores the findings with the judge, and appends one JSON object
to data/runs/<timestamp>.jsonl.

Hygiene:
* Resumable — a cell already present in the JSONL is skipped, so a crash never
  means paying twice.
* Hard spend cap — before each trial, the running cost estimate is checked
  against budget.max_usd and the run halts cleanly if the next trial would exceed it.
* --dry-run prints the full plan and does NOT call any model (free).

Spends API budget only when run without --dry-run and with a key configured.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any

from . import agent, scorer
from .hints import get_hint
from .pricing import usd_for_usage


def _load_config(path: str) -> dict[str, Any]:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _enabled_models(cfg: dict) -> list[dict]:
    return [m for m in cfg.get("models", []) if m.get("enabled")]


def _enabled_levels(cfg: dict) -> list[str]:
    return [lvl for lvl, on in cfg.get("hint_levels", {}).items() if on]


def _cell_key(model: str, level: str, trial: int) -> str:
    return f"{model}|{level}|{trial}"


def _load_done(jsonl_path: str) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(jsonl_path):
        return done
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done.add(_cell_key(rec["model"], rec["level"], rec["trial"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _usage_usd(model: str, trial_result: dict, score: dict) -> float:
    total = usd_for_usage(model, trial_result.get("usage", {}))
    for v in score.get("per_finding", []):
        total += usd_for_usage(model, v.get("_usage", {}))  # judge billed to same model id budget
    return total


def recall_probe(client, cfg: dict, model: str, answer_key: dict) -> dict:
    """Ask the model about the CVEs with no tools; flag contamination."""
    probe = cfg.get("recall_probe", {})
    questions = probe.get("questions", [])
    answers = []
    for q in questions:
        resp = client.messages.create(
            model=model, max_tokens=600,
            messages=[{"role": "user", "content": q}],
        )
        answers.append("".join(getattr(b, "text", "") for b in resp.content
                               if getattr(b, "type", None) == "text"))
    # Judge whether the model recited a target mechanism.
    joined = {"title": "recall probe", "subsystem": "", "mechanism": " ".join(answers),
              "reachable_how": ""}
    verdict = scorer.judge_finding(client, model=cfg["judge"]["id"],
                                   answer_key=answer_key, finding=joined)
    contaminated = any((verdict.get(t["id"]) or {}).get("verdict") == "HIT"
                       for t in answer_key["targets"])
    return {"contaminated": contaminated, "answers": answers, "verdict": verdict}


def run(config_path: str, *, dry_run: bool = False, limit: "int | None" = None,
        skip_probe: bool = False, out: "str | None" = None,
        trials_override: "int | None" = None) -> str:
    cfg = _load_config(config_path)
    models = _enabled_models(cfg)
    levels = _enabled_levels(cfg)
    trials = int(trials_override if trials_override is not None else cfg.get("trials", 10))
    cap = float(cfg.get("budget", {}).get("max_usd", 0) or 0)
    root = cfg["target"]["root"]
    runs_dir = cfg["paths"]["runs_dir"]
    agent_cfg = cfg.get("agent", {})

    plan = [(m, lvl, t) for m in models for lvl in levels for t in range(trials)]
    print(f"Plan: {len(models)} model(s) x {len(levels)} level(s) x {trials} "
          f"trial(s) = {len(plan)} cells")
    for m in models:
        print(f"  model {m['id']}  (cutoff {m.get('cutoff')})")
    print(f"  levels: {levels}")
    print(f"  spend cap: ${cap:.2f}" if cap else "  spend cap: none")

    if dry_run:
        print("\n--dry-run: no model calls made.")
        return ""

    import anthropic
    client = anthropic.Anthropic()
    answer_key = scorer.load_answer_key(cfg.get("answer_key", "answer_key.yaml"))

    os.makedirs(runs_dir, exist_ok=True)
    if out:
        out_path = out
    else:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(runs_dir, f"{stamp}.jsonl")
    done = _load_done(out_path)
    if done:
        print(f"Resuming {out_path}: {len(done)} cell(s) already done, will skip them.")
    spent = 0.0
    n_run = 0

    # Recall probe per model (once).
    contaminated_models: set[str] = set()
    if cfg.get("recall_probe", {}).get("enabled") and not skip_probe:
        for m in models:
            probe = recall_probe(client, cfg, m["id"], answer_key)
            spent += usd_for_usage(cfg["judge"]["id"], probe["verdict"].get("_usage", {}))
            tag = "CONTAMINATED" if probe["contaminated"] else "clean"
            print(f"recall probe {m['id']}: {tag}")
            if probe["contaminated"] and cfg["recall_probe"].get("drop_if_recalled"):
                contaminated_models.add(m["id"])

    with open(out_path, "a", encoding="utf-8") as out:
        for model_cfg, level, trial in plan:
            model = model_cfg["id"]
            if model in contaminated_models:
                continue
            key = _cell_key(model, level, trial)
            if key in done:
                continue
            if cap and spent >= cap:
                print(f"\nSpend cap ${cap:.2f} reached (est ${spent:.2f}); stopping.")
                break
            if limit is not None and n_run >= limit:
                print(f"\n--limit {limit} reached; stopping.")
                break

            result = agent.run_trial(
                client, model=model, hint_text=get_hint(level), repo_root=root,
                max_tool_calls=int(agent_cfg.get("max_tool_calls", 40)),
                max_tokens=int(agent_cfg.get("max_tokens", 8000)),
                effort=agent_cfg.get("effort", "high"),
            )
            if result["stopped"] == "refusal":
                # The model declined the task; there is nothing to judge. Mark the
                # targets REFUSED so analysis can separate "won't engage" from a
                # genuine MISS, and skip the (billable) judge call.
                score = {"t618": "REFUSED", "t619": "REFUSED",
                         "n_false_positives": 0, "false_positive_classes": [],
                         "per_finding": []}
            else:
                score = scorer.score_trial(
                    result["findings"],
                    lambda f: scorer.judge_finding(client, model=cfg["judge"]["id"],
                                                   answer_key=answer_key, finding=f),
                    answer_key,
                )
            cost = _usage_usd(model, result, score)
            spent += cost
            n_run += 1

            record = {
                "model": model, "cutoff": model_cfg.get("cutoff"),
                "level": level, "trial": trial,
                "t618": score["t618"], "t619": score["t619"],
                "n_false_positives": score["n_false_positives"],
                "false_positive_classes": score["false_positive_classes"],
                "effort": result["effort"], "usage": result["usage"],
                "stopped": result["stopped"], "est_usd": round(cost, 4),
                "findings": result["findings"], "scores": score["per_finding"],
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(f"{key}: t618={score['t618']} t619={score['t619']} "
                  f"fp={score['n_false_positives']} calls={result['effort']['tool_calls']} "
                  f"${cost:.3f} (cum ${spent:.2f})")

    print(f"\nWrote {out_path}  (est total ${spent:.2f})")
    return out_path


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description="Run the PHP2Shell autonomy matrix.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no model calls")
    ap.add_argument("--limit", type=int, default=None, help="cap trials this invocation")
    ap.add_argument("--no-probe", action="store_true", help="skip the recall probe")
    ap.add_argument("--out", default=None, help="JSONL file to resume/append (default: new timestamped)")
    ap.add_argument("--trials", type=int, default=None, help="override trials per cell")
    args = ap.parse_args(argv)
    run(args.config, dry_run=args.dry_run, limit=args.limit, skip_probe=args.no_probe,
        out=args.out, trials_override=args.trials)


if __name__ == "__main__":
    main()
