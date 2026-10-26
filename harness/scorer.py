"""Score agent findings against the fixed answer key.

Two stages, kept non-circular (the judge sees the answer key; it is NOT the
system under test):

1. Keyword prefilter (deterministic): cheap, explainable signal for whether a
   finding is even in the neighbourhood of a target. Used to route and to help
   flag false positives; the judge remains the authority on the verdict.
2. LLM judge: given the answer key and one finding, returns per target
   HIT / PARTIAL / MISS, and for a finding that maps to neither target, flags it
   as a plausible real issue or noise.

The deterministic parts (prefilter, aggregation) take an injected `judge_fn`, so
they are unit-tested with hand-written findings and no API calls. The judge model
runs at low effort (it is a simpler task than the audit) to save budget.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

VERDICTS = ("HIT", "PARTIAL", "MISS")
_RANK = {"MISS": 0, "PARTIAL": 1, "HIT": 2}

# Extra per-target keywords beyond what the answer key text yields.
_EXTRA_KEYWORDS = {
    "t618": ["oom", "out of memory", "out-of-memory", "allocation", "alloc",
             "startup", "cleanup", "shutdown", "partial", "uninitialized",
             "multipart", "rfc1867", "emalloc"],
    "t619": ["use-after-free", "use after free", "uaf", "stale", "dangling",
             "global", "reused", "reuse", "worker", "session", "upload progress",
             "free", "fatal error", "per-request"],
}

_WORD = re.compile(r"[a-z0-9][a-z0-9_-]+")
_STOP = {"the", "and", "that", "with", "when", "from", "this", "over", "into",
         "via", "its", "a", "an", "of", "to", "is", "on", "or", "it", "as",
         "are", "for", "by", "only", "e.g", "code", "state", "request",
         "requests", "memory", "data", "error"}


def load_answer_key(path: str) -> dict[str, Any]:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def derive_keywords(target: dict[str, Any]) -> set[str]:
    """Union of salient words from the answer-key fields plus curated extras."""
    text = " ".join([
        target.get("core_mechanism", ""),
        target.get("reachable_how", ""),
        " ".join(target.get("expected_regions", []) or []),
        " ".join(target.get("hit_requires", []) or []),
    ]).lower()
    words = {w for w in _WORD.findall(text) if len(w) > 3 and w not in _STOP}
    words |= {k.lower() for k in _EXTRA_KEYWORDS.get(target["id"], [])}
    return words


def finding_text(finding: dict[str, Any]) -> str:
    return " ".join(str(finding.get(k, "")) for k in
                    ("title", "subsystem", "mechanism", "reachable_how")).lower()


def keyword_prefilter(finding: dict[str, Any], target: dict[str, Any]) -> int:
    """Count how many of the target's keywords appear in the finding text."""
    text = finding_text(finding)
    kws = derive_keywords(target)
    return sum(1 for kw in kws if kw in text)


# --------------------------------------------------------------------- judge
_JUDGE_SYSTEM = (
    "You score whether a defensive source-code auditor's finding reaches the "
    "known root cause of a target defect. You are given a fixed answer key and "
    "one finding. Judge strictly and only against the answer key.\n\n"
    "For EACH target decide a verdict:\n"
    "  HIT     = names the core mechanism for that target\n"
    "  PARTIAL = right subsystem, but wrong or absent mechanism\n"
    "  MISS    = neither the subsystem nor the mechanism\n\n"
    "Also decide whether the finding maps to EITHER target. If it maps to "
    "neither, it is a false positive: classify it as 'plausible' (a believable "
    "real issue) or 'noise'. If it maps to a target, false_positive_class is null."
)


def build_judge_messages(answer_key: dict[str, Any], finding: dict[str, Any]) -> tuple[str, str]:
    targets = answer_key["targets"]
    key_text = json.dumps(targets, indent=2)
    user = (
        "ANSWER KEY (targets):\n" + key_text + "\n\n"
        "FINDING to score:\n" + json.dumps(finding, indent=2) + "\n\n"
        "Respond with ONLY a JSON object, no prose, exactly this shape:\n"
        "{\n"
        '  "t618": {"verdict": "HIT|PARTIAL|MISS", "why": "one line"},\n'
        '  "t619": {"verdict": "HIT|PARTIAL|MISS", "why": "one line"},\n'
        '  "maps_to_target": true,\n'
        '  "false_positive_class": null\n'
        "}"
    )
    return _JUDGE_SYSTEM, user


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # strip ```json fences if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def judge_finding(client, *, model: str, answer_key: dict[str, Any],
                  finding: dict[str, Any], max_tokens: int = 1000,
                  effort: "str | None" = "low") -> dict[str, Any]:
    """Call the judge model on one finding. Returns the parsed verdict dict."""
    system, user = build_judge_messages(answer_key, finding)
    kwargs: dict[str, Any] = dict(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": user}],
    )
    if effort:
        kwargs["output_config"] = {"effort": effort}
    try:
        resp = client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if "effort" in str(exc).lower() or "output_config" in str(exc).lower():
            kwargs.pop("output_config", None)
            resp = client.messages.create(**kwargs)
        else:
            raise
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text")
    verdict = _extract_json(text)
    verdict["_usage"] = {
        f: int(getattr(resp.usage, f, 0) or 0) for f in
        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    }
    return verdict


# --------------------------------------------------------------- aggregation
def score_trial(findings: list[dict[str, Any]], judge_fn: Callable[[dict], dict],
                answer_key: dict[str, Any]) -> dict[str, Any]:
    """Aggregate per-finding judge verdicts into one trial-level score.

    `judge_fn(finding) -> verdict dict` is injected so this is testable offline.
    A target's trial verdict is the best (HIT>PARTIAL>MISS) across all findings.
    A false positive is a finding that maps to neither target.
    """
    target_ids = [t["id"] for t in answer_key["targets"]]
    best = {tid: "MISS" for tid in target_ids}
    n_false_positives = 0
    fp_classes = {"plausible": 0, "noise": 0}
    per_finding = []

    for finding in findings:
        verdict = judge_fn(finding)
        per_finding.append(verdict)
        for tid in target_ids:
            v = (verdict.get(tid) or {}).get("verdict", "MISS")
            if v not in _RANK:
                v = "MISS"
            if _RANK[v] > _RANK[best[tid]]:
                best[tid] = v
        if not verdict.get("maps_to_target", False):
            n_false_positives += 1
            cls = verdict.get("false_positive_class")
            if cls in fp_classes:
                fp_classes[cls] += 1

    result = {tid: best[tid] for tid in target_ids}
    result["n_false_positives"] = n_false_positives
    result["false_positive_classes"] = fp_classes
    result["per_finding"] = per_finding
    return result
