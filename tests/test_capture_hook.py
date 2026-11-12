"""Hook de parada (captura de fim de sessão): opt-in, idempotente e sem tocar nos hooks de terceiros."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ctx.policy import CAPTURE_MARKER, capture_hook_command, sync_rules, validate_claude_hooks

THIRD_PARTY_STOP = {"hooks": [{"type": "command", "command": "notify-send terminou"}]}


class TestStopHook(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = self.root / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"hooks": {"Stop": [THIRD_PARTY_STOP]}}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def policy(self, capture: str, runner: str = ".venv/bin/ctx") -> None:
        (self.root / "policy.toml").write_text(
            f'[ctx]\nrunner = "{runner}"\n[harnesses]\nenabled = ["claude"]\n[recall]\ncapture_on_stop = {capture}\n', encoding="utf-8"
        )

    def stop_entries(self) -> list[dict]:
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"].get("Stop", [])

    def test_off_by_default_leaves_third_party_alone(self) -> None:
        self.policy("false")
        sync_rules(self.root)
        self.assertEqual(self.stop_entries(), [THIRD_PARTY_STOP])

    def test_on_registers_capture_and_keeps_third_party(self) -> None:
        self.policy("true")
        sync_rules(self.root)
        entries = self.stop_entries()
        self.assertIn(THIRD_PARTY_STOP, entries)
        ours = [e for e in entries if CAPTURE_MARKER in json.dumps(e)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["hooks"][0]["command"], '"$CLAUDE_PROJECT_DIR/.venv/bin/ctx" recall capture --from-hook')
        self.assertTrue(validate_claude_hooks(json.loads(self.settings.read_text(encoding="utf-8")))[0])

    def test_idempotent_and_removable(self) -> None:
        self.policy("true")
        sync_rules(self.root)
        sync_rules(self.root)
        self.assertEqual(sum(CAPTURE_MARKER in json.dumps(e) for e in self.stop_entries()), 1)
        self.policy("false")
        sync_rules(self.root)
        self.assertEqual(self.stop_entries(), [THIRD_PARTY_STOP])

    def test_stop_event_removed_entirely_when_only_ours(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        self.policy("true")
        sync_rules(self.root)
        self.policy("false")
        sync_rules(self.root)
        self.assertNotIn("Stop", json.loads(self.settings.read_text(encoding="utf-8"))["hooks"])

    def test_guardrail_hook_still_registered(self) -> None:
        self.policy("true")
        sync_rules(self.root)
        pre = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.assertTrue(any("scripts/guardrail.py" in json.dumps(e) for e in pre))

    def test_command_for_other_runners(self) -> None:
        self.assertEqual(capture_hook_command("ctx"), f'"ctx" {CAPTURE_MARKER}')
        self.assertEqual(capture_hook_command("/opt/ctx/bin/ctx"), f'"/opt/ctx/bin/ctx" {CAPTURE_MARKER}')
        self.assertEqual(capture_hook_command("uv run ctx"), f'"uv" run ctx {CAPTURE_MARKER}')

    def test_instructions_mention_new_commands(self) -> None:
        self.policy("false")
        sync_rules(self.root)
        text = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        for needle in ("recall", "fact add", "upstream get", "recipe run", "doctor --fix", "evaluate"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
