"""Guardrail: sugere `ctx recipe run` quando o agente repete à mão a sequência de uma receita (nunca bloqueia)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctx.metrics import guardrail_module

POLICY = {
    "ctx": {"runner": ".venv/bin/ctx"},
    "recipes": {"deploy": {"params": ["alvo"], "steps": ["make build", "scp out/{alvo} host:/srv", {"run": "ssh host restart {alvo}"}]}},
}


class TestHint(unittest.TestCase):
    def setUp(self) -> None:
        self.g = guardrail_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(self.g, "WORKSPACE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_builtin_copy_verify_sequence(self) -> None:
        self.assertIsNone(self.g.record_and_suggest("cp a.bin /mnt/x/a.bin", {}))
        hint = self.g.record_and_suggest("sync", {})
        self.assertIn("recipe run copy-verify <origem> <destino>", hint)
        self.assertIn(".venv/bin/ctx", hint)

    def test_policy_recipe_any_consecutive_pair(self) -> None:
        self.assertIsNone(self.g.record_and_suggest("git status", POLICY))
        self.assertIsNone(self.g.record_and_suggest("scp out/app host:/srv", POLICY))
        hint = self.g.record_and_suggest("ssh host restart app", POLICY)
        self.assertIn("`deploy`", hint)
        self.assertIn("<alvo>", hint)

    def test_unrelated_or_out_of_order_commands_do_not_trigger(self) -> None:
        for cmd in ("ls", "sync", "cp a b"):
            self.assertIsNone(self.g.record_and_suggest(cmd, {}), cmd)

    def test_old_history_is_ignored(self) -> None:
        self.g.record_and_suggest("cp a b", {})
        path = self.root / ".ctx" / "hint-history.json"
        hist = json.loads(path.read_text(encoding="utf-8"))
        hist[0]["ts"] -= 10_000
        path.write_text(json.dumps(hist), encoding="utf-8")
        self.assertIsNone(self.g.record_and_suggest("sync", {}))

    def test_running_the_recipe_is_not_recorded(self) -> None:
        self.g.record_and_suggest("cp a b", {})
        self.assertIsNone(self.g.record_and_suggest(".venv/bin/ctx recipe run copy-verify a b", {}))
        self.assertIsNotNone(self.g.record_and_suggest("sync", {}))  # histórico anterior intacto

    def test_hint_is_logged_for_metrics(self) -> None:
        self.g.record_and_suggest("cp a b", {})
        self.g.record_and_suggest("sync", {})
        events = [json.loads(line) for line in (self.root / ".ctx" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["cmd"], e["recipe"]) for e in events], [("hint", "copy-verify")])

    def test_never_raises_when_workspace_is_read_only(self) -> None:
        with mock.patch.object(self.g, "WORKSPACE_ROOT", Path("/proc/nao-existe")):
            self.assertIsNone(self.g.record_and_suggest("cp a b", {}))

    def test_with_hint_keeps_decision(self) -> None:
        hint = "Dica: x"
        allow = self.g.with_hint(self.g.format_response("claude", "allow", "ok"), "allow", hint, "claude")
        self.assertEqual(allow["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(allow["hookSpecificOutput"]["additionalContext"], hint)
        defer = self.g.with_hint(self.g.format_response("claude", "defer", "x"), "defer", hint, "claude")
        self.assertEqual(defer, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": hint}})
        ask = self.g.with_hint(self.g.format_response("claude", "ask", "confirme"), "ask", hint, "claude")
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertTrue(ask["hookSpecificOutput"]["permissionDecisionReason"].endswith(hint))
        gem = self.g.with_hint(self.g.format_response("gemini", "allow", "ok"), "allow", hint, "gemini")
        self.assertEqual(gem, {"decision": "allow"})  # harness sem canal de contexto: não altera a resposta
        self.assertEqual(self.g.with_hint({"x": 1}, "allow", None, "claude"), {"x": 1})


if __name__ == "__main__":
    unittest.main()
