"""Offline validation of the agent loop — no API, no network.

A fake Anthropic client scripts the responses, so we exercise the loop, tool
dispatch, the submit_findings terminal path, the tool-call budget, and the cache
placement without spending any budget.
"""

import copy
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent  # noqa: E402


def block(**kw):
    return types.SimpleNamespace(**kw)


def tool_use(name, tid, inp):
    return block(type="tool_use", name=name, id=tid, input=inp)


def usage(**kw):
    base = dict(input_tokens=10, output_tokens=5,
                cache_creation_input_tokens=0, cache_read_input_tokens=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


class FakeClient:
    """Replays a scripted list of responses; records every request's kwargs."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        # Snapshot: the loop mutates the messages list in place after this call.
        self.calls.append(copy.deepcopy(kwargs))
        if self._scripted:
            return self._scripted.pop(0)
        # default: keep asking to list_dir (used by the budget test)
        return block(
            content=[tool_use("list_dir", f"t{len(self.calls)}", {"path": "."})],
            stop_reason="tool_use", usage=usage(),
        )


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "main").mkdir()
        (root / "main" / "php.h").write_text("line1\nline2\nline3\n")
        self.root = str(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_happy_path_submits_findings(self):
        scripted = [
            block(content=[tool_use("list_dir", "a", {"path": "."})],
                  stop_reason="tool_use", usage=usage()),
            block(content=[tool_use("read_file", "b", {"path": "main/php.h"})],
                  stop_reason="tool_use", usage=usage(cache_read_input_tokens=200)),
            block(content=[tool_use("submit_findings", "c", {"findings": [
                {"title": "x", "subsystem": "main", "mechanism": "m", "reachable_how": "r"}]})],
                  stop_reason="tool_use", usage=usage()),
        ]
        client = FakeClient(scripted)
        out = agent.run_trial(client, model="claude-opus-4-8", hint_text="hint",
                              repo_root=self.root, max_tool_calls=40)
        self.assertEqual(out["stopped"], "submit")
        self.assertEqual(out["n_findings"], 1)
        self.assertEqual(out["effort"]["tool_calls"], 2)
        self.assertEqual(out["effort"]["files_read"], 1)
        # usage accumulated across all three turns, including the cache read
        self.assertEqual(out["usage"]["cache_read_input_tokens"], 200)

    def test_system_prefix_and_rolling_cache_are_set(self):
        scripted = [
            block(content=[tool_use("submit_findings", "c", {"findings": []})],
                  stop_reason="tool_use", usage=usage()),
        ]
        client = FakeClient(scripted)
        agent.run_trial(client, model="claude-opus-4-8", hint_text="hint", repo_root=self.root)
        first = client.calls[0]
        # system prefix cached (1h)
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        # rolling breakpoint on the last user content block
        self.assertIn("cache_control", first["messages"][-1]["content"][-1])
        # never exceed the 4-breakpoint budget
        for call in client.calls:
            n = sum(1 for m in call["messages"] if isinstance(m.get("content"), list)
                    for b in m["content"] if isinstance(b, dict) and "cache_control" in b)
            self.assertLessEqual(n + 1, 4)  # +1 for the system breakpoint

    def test_budget_forces_submit_and_caps_tool_calls(self):
        # Never submits on its own -> the budget nudge must kick in.
        client = FakeClient([])  # falls through to the default list_dir responder
        out = agent.run_trial(client, model="claude-opus-4-8", hint_text="hint",
                              repo_root=self.root, max_tool_calls=3)
        self.assertLessEqual(out["effort"]["tool_calls"], 3)
        self.assertIn(out["stopped"], ("budget", "cap", "end_turn"))
        # a budget nudge text block was appended to a tool-result message
        nudged = any(
            isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "text"
                    and "budget" in b.get("text", "").lower() for b in m["content"])
            for call in client.calls for m in call["messages"]
        )
        self.assertTrue(nudged)

    def test_prose_answer_is_captured_as_finding(self):
        # The model answers in text with no tool call (the opus-5-5 L3 failure).
        # It cannot be forced into a tool, so the loop must capture the prose and
        # score it, not exit with zero findings.
        scripted = [
            block(content=[block(type="text",
                                 text="The root cause is an OOM-driven heap corruption in main.")],
                  stop_reason="end_turn", usage=usage()),
        ]
        client = FakeClient(scripted)
        out = agent.run_trial(client, model="claude-opus-5-5", hint_text="hint",
                              repo_root=self.root)
        self.assertEqual(out["stopped"], "prose")
        self.assertEqual(out["n_findings"], 1)
        self.assertIn("OOM-driven heap corruption", out["findings"][0]["mechanism"])
        self.assertEqual(out["effort"]["tool_calls"], 0)
        # never attempts a forced tool_choice (unsupported on this model)
        for call in client.calls:
            self.assertNotIn("tool_choice", call)

    def test_refusal_is_recorded_distinctly(self):
        # The safety layer declines: stop_reason="refusal", no content blocks.
        scripted = [block(content=[], stop_reason="refusal", usage=usage())]
        client = FakeClient(scripted)
        out = agent.run_trial(client, model="claude-opus-5-5", hint_text="hint",
                              repo_root=self.root)
        self.assertEqual(out["stopped"], "refusal")
        self.assertEqual(out["n_findings"], 0)
        self.assertEqual(out["effort"]["tool_calls"], 0)

    def test_param_fallback_on_rejection(self):
        class PickyClient(FakeClient):
            def create(self, **kwargs):
                if "thinking" in kwargs or "output_config" in kwargs:
                    raise RuntimeError("thinking is not supported for this model")
                return super().create(**kwargs)

        scripted = [block(content=[tool_use("submit_findings", "c", {"findings": []})],
                          stop_reason="tool_use", usage=usage())]
        client = PickyClient(scripted)
        out = agent.run_trial(client, model="legacy-model-xyz", hint_text="hint",
                              repo_root=self.root, thinking=True, effort="high")
        self.assertTrue(out["params_stripped"])
        self.assertEqual(out["stopped"], "submit")

    def test_no_code_field_in_submit_schema(self):
        submit = next(t for t in agent.TOOLS if t["name"] == "submit_findings")
        props = submit["input_schema"]["properties"]["findings"]["items"]["properties"]
        self.assertNotIn("code", props)
        self.assertEqual(
            sorted(props), ["mechanism", "reachable_how", "subsystem", "title"]
        )
        self.assertTrue(submit["input_schema"]["properties"]["findings"]["items"]["additionalProperties"] is False)


if __name__ == "__main__":
    unittest.main()
