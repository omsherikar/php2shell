# PHP2Shell Autonomy Experiment

A reproducibility study that measures **how much human steering an AI model needs
to rediscover the public root causes** of two already-disclosed, already-patched
PHP vulnerabilities (CVE-2026-109618, CVE-2026-109619, fixed 2026-10-21). It is
defensive measurement, not bug-hunting: the harness varies only the strength of
the hint the model is given and scores whether it reaches the two published
root-cause mechanisms.

---

## Scope and safety guardrails (load-bearing — do not relax)

1. **Read-only agent.** Tools are `list_dir`, `read_file`, `grep` only. No write, no shell, no code
   execution, no network. The agent can read source; it cannot run or modify anything.
2. **Root-cause only.** The agent is scored on *identifying and explaining* the defect's mechanism.
   The system prompt explicitly tells it **not** to write exploit code, PoCs, or payloads, and the
   terminal `submit_findings` tool accepts a mechanism description, not code.
3. **No future-leak.** The target is checked out at a pre-fix tag, and the tools refuse any path
   containing `.git`, so the model can't read the fix commit, the fixed NEWS entry, or branch history.
4. **Measurement, not hunting.** Fixed target, fixed answer key derived from the public advisories.
   The harness is not a general vulnerability scanner and should not be repurposed as one.

---

## What this is (and is not)

- **Is:** a controlled measurement of model autonomy on a *known-answer* task. Three conflicting
  public claims exist about the PHP2Shell disclosures — that frontier AI found them unaided, that
  humans pointed the AI at the right code, and that a basic prompt reproduced them. Nobody has
  *measured* it. This harness does, by holding everything constant except one variable: the hint.
- **Is not:** a vulnerability scanner, an exploit generator, or a tool for finding *new* bugs. The
  target and the answer key are fixed. The agent never produces exploit code — it writes prose
  describing a mechanism, which is then scored against the public advisories.

## Target under study

- **Repo:** `php/php-src`
- **Checkout:** tag `php-8.5.11` (the release immediately before the fix). The 2026-10-21 fix commit
  and its CVE-referencing NEWS entry are **not** present at this tag.
- **Setup:** `./setup_target.sh` clones and checks out the tag into `data/target/php-src`. The tools
  hard-block any path containing `.git`.

## Layout

```
README.md                  # this file (guardrails 1-4 at the top)
requirements.txt           # anthropic, pyyaml
config.yaml                # models (+cutoffs), hint levels, trials, paths
answer_key.yaml            # the two target root causes + scoring rubric   (step 4)
setup_target.sh            # clone + checkout php-8.5.11
harness/
  tools.py                 # read-only repo tools + path guard (.git blocked)   [this step]
  hints.py                 # the 4 hint-level prompt texts                 (step 4)
  agent.py                 # agentic loop over Anthropic tool-use          (step 5)
  scorer.py                # findings -> targets via judge + keyword prefilter  (step 7)
  run.py                   # orchestrates the matrix, writes JSONL         (step 8)
  analyze.py               # JSONL -> summary table + figures              (step 9)
tests/
  test_tools.py            # path-guard unit tests (.git + escape rejected) [this step]
data/
  target/php-src/          # the checkout (gitignored)
  runs/                    # raw JSONL output, one line per trial
  results/                 # summary.csv + figures
```

## How to run

> The study spends API budget. Nothing below calls a model until you set a key and run `harness.run`.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # or your preferred secret handling
./setup_target.sh                   # clone + checkout php-8.5.11

# run the path-guard unit tests (no API, no network):
python -m unittest discover -s tests -v

# later phases (not built yet):
# python -m harness.run --config config.yaml
# python -m harness.analyze --runs data/runs/<timestamp>.jsonl
```

## Spending (token budget)

The study's cost is dominated by the agent re-sending its growing history every
turn (~40 tool calls per trial x trials x models). The harness is built to keep
that cheap, and model choice is NOT a cost knob — the models under test are fixed
by the research design (training cutoff before 2026-10-21).

- **Prompt caching.** The frozen system + tools prefix is cached (1h TTL) and a
  rolling breakpoint tracks the conversation, so each turn re-sends history as
  cache reads (~0.1x) not full-price input. `agent.py` logs
  `cache_read_input_tokens` per trial — if it stays 0, caching is broken.
- **Capped tool output.** reads 400 lines, grep 100 matches, 300 chars/line
  (`tools.py`). Keeps the uncached tail small, compounding with caching.
- **Batch the judge.** Scoring calls (step 7) are single-shot and independent, so
  they go through the Message Batches API at 50% off. The agent loop is
  interactive and cannot batch.
- **Pilot, then fill.** Run ~2 trials per cell first; spend the full 8-10 only on
  cells near the interesting threshold. Saturated cells (0% / 100%) need few.
- **Effort is fixed, not a cost knob.** Effort changes what the agent can do —
  the thing being measured — so it is held constant and logged, never lowered to
  save money.
- **Resume.** `run.py` (step 8) skips cells already in the JSONL, so a crash
  never means paying twice.

## Build status

- [x] 1. Scaffold + README (guardrails) + `requirements.txt` + `config.yaml`
- [x] 2. `setup_target.sh` — clone php-src, checkout `php-8.5.11`
- [x] 3. `harness/tools.py` + path-guard unit tests
- [x] 4. `answer_key.yaml` + `harness/hints.py` (pinned stimulus + fixed key)
- [x] 5. `harness/agent.py` (tool-use loop + caching + submit_findings; mock-tested)
- [x] 6. Smoke test (one L0 run, one model) — done; caching verified, guardrails held
- [x] 7. `harness/scorer.py` (keyword prefilter + judge; offline-tested)
- [x] 8. `harness/run.py` (matrix + resume + recall probe + spend cap; `--dry-run` verified)
- [x] 9. `harness/analyze.py` (summary.csv + 3 figures; verified on synthetic data)
- [x] 10. Hand sanity-check of HIT/MISS judgments — done; judge sound on the L0/L1
      floor and clear L3 hits, with a modest upward bias on a few L2 t618 HITs
