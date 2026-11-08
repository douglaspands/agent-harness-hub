"""Testes unitários para o sincronizador de políticas, cache de binários e diagnóstico."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ctx.policy import doctor, load_policy, resolve_binaries, sync_rules, validate_claude_hooks


class TestPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.ctx_dir = self.workspace / ".ctx"
        self.ctx_dir.mkdir(parents=True, exist_ok=True)

        self.policy_file = self.workspace / "policy.toml"
        self.policy_file.write_text(
            """
[project]
name = "test-hub"

[commands]
allow = ["git status", "uv run"]
ask = ["git push"]
deny = ["rm -rf /"]
safe_pipe_filters = ["rg", "head"]

[commands.one_shot_recipes]
ctx_status = ".venv/bin/ctx status"

[toolchains.python]
canonical_runner = "uv run"
sandbox_runners = [".venv/bin/ctx", ".venv/bin/python"]
forbidden_direct = ["python3"]
""",
            encoding="utf-8",
        )

        # Mock de guardrail
        scripts_dir = self.workspace / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "guardrail.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        venv_python = scripts_dir / "venv-python"
        venv_python.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        venv_python.chmod(0o755)

        # Mock de index.db
        (self.ctx_dir / "index.db").touch()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_policy(self) -> None:
        pol = load_policy(self.policy_file)
        self.assertEqual(pol["project"]["name"], "test-hub")
        self.assertIn("git status", pol["commands"]["allow"])

    def test_resolve_binaries(self) -> None:
        bins = resolve_binaries(workspace_root=self.workspace, refresh=True)
        self.assertIn("binaries", bins)
        self.assertIn("git", bins["binaries"])
        self.assertTrue((self.ctx_dir / "bins.json").exists())

    def test_sync_rules(self) -> None:
        res = sync_rules(workspace_root=self.workspace)
        self.assertEqual(res["status"], "success")

        # Verifica criação dos arquivos de hook e docs
        self.assertTrue((self.workspace / ".agents" / "hooks.json").exists())
        self.assertTrue((self.workspace / ".claude" / "settings.json").exists())
        self.assertTrue((self.workspace / "AGENTS.md").exists())
        self.assertTrue((self.workspace / "CLAUDE.md").exists())
        self.assertTrue((self.workspace / "GEMINI.md").exists())

    def test_doctor(self) -> None:
        sync_rules(workspace_root=self.workspace)
        doc_res = doctor(workspace_root=self.workspace)
        self.assertTrue(doc_res["all_passed"], doc_res["checks"])
        self.assertEqual(doc_res["status"], "healthy")

    def test_sync_writes_valid_claude_hook_format(self) -> None:
        sync_rules(workspace_root=self.workspace)
        settings = json.loads((self.workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))
        entries = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], "")
        self.assertIsInstance(entries[0]["hooks"], list)
        self.assertEqual(entries[0]["hooks"][0]["type"], "command")
        command = entries[0]["hooks"][0]["command"]
        self.assertIn("scripts/guardrail.py", command)
        # Nunca o python global: o hook passa pelo launcher do .venv
        self.assertTrue(command.startswith('"$CLAUDE_PROJECT_DIR/scripts/venv-python"'), command)
        self.assertNotIn("python3", command)
        self.assertTrue(validate_claude_hooks(settings)[0])

    def test_sync_migrates_legacy_entry_and_preserves_keys(self) -> None:
        claude_dir = self.workspace / ".claude"
        claude_dir.mkdir()
        legacy = {
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "PreToolUse": [
                    {"command": "python3 scripts/guardrail.py"},
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo outro"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "notify-send ok"}]}],
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")

        # O formato legado é detectado pelo doctor
        self.assertFalse(validate_claude_hooks(legacy)[0])

        sync_rules(workspace_root=self.workspace)
        settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], legacy["permissions"])
        self.assertIn("Stop", settings["hooks"])
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertNotIn("command", pre[0])
        self.assertEqual(pre[1]["matcher"], "Bash")
        self.assertTrue(validate_claude_hooks(settings)[0])

    def test_sync_is_idempotent(self) -> None:
        sync_rules(workspace_root=self.workspace)
        first = (self.workspace / ".claude" / "settings.json").read_text(encoding="utf-8")
        sync_rules(workspace_root=self.workspace)
        self.assertEqual(first, (self.workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_sync_all_harnesses_and_merge(self) -> None:
        text = self.policy_file.read_text(encoding="utf-8")
        self.policy_file.write_text(
            text + '\n[harnesses]\nenabled = ["claude", "codex", "cursor", "gemini", "antigravity"]\n', encoding="utf-8"
        )
        # Configs pré-existentes de terceiros devem ser preservadas
        (self.workspace / ".cursor").mkdir()
        (self.workspace / ".cursor" / "hooks.json").write_text(
            json.dumps({"version": 1, "hooks": {"afterFileEdit": [{"command": ".cursor/hooks/format.sh"}]}}), encoding="utf-8"
        )
        (self.workspace / ".gemini").mkdir()
        (self.workspace / ".gemini" / "settings.json").write_text(json.dumps({"theme": "Dracula"}), encoding="utf-8")

        res = sync_rules(workspace_root=self.workspace)
        for rel in (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json", ".gemini/settings.json", ".agents/hooks.json"):
            self.assertIn(rel, res["synced_targets"])

        codex = json.loads((self.workspace / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        entry = codex["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "^(Bash|apply_patch)$")
        self.assertIn("git rev-parse --show-toplevel", entry["hooks"][0]["command"])
        self.assertIn("scripts/venv-python", entry["hooks"][0]["command"])

        cursor = json.loads((self.workspace / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["version"], 1)
        self.assertIn("afterFileEdit", cursor["hooks"])
        self.assertEqual(cursor["hooks"]["beforeShellExecution"][0]["command"], "scripts/venv-python scripts/guardrail.py")
        self.assertIn("beforeReadFile", cursor["hooks"])

        gemini = json.loads((self.workspace / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(gemini["theme"], "Dracula")
        self.assertIn("$GEMINI_PROJECT_DIR", gemini["hooks"]["BeforeTool"][0]["hooks"][0]["command"])

        # Idempotente: nenhum hook duplicado após 2º sync
        sync_rules(workspace_root=self.workspace)
        cursor2 = json.loads((self.workspace / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cursor2["hooks"]["beforeShellExecution"]), 1)

        doc = doctor(workspace_root=self.workspace)
        self.assertTrue(doc["all_passed"], doc["checks"])
        names = {c["name"] for c in doc["checks"]}
        self.assertTrue({"Hook Codex válido", "Hook Cursor válido", "Hook Gemini CLI válido"} <= names)

    def test_harness_specific_instructions(self) -> None:
        sync_rules(workspace_root=self.workspace)
        claude_md = (self.workspace / "CLAUDE.md").read_text(encoding="utf-8")
        agents_md = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        gemini_md = (self.workspace / "GEMINI.md").read_text(encoding="utf-8")
        self.assertIn("ferramenta `Read`", claude_md)
        self.assertIn("apply_patch", agents_md)
        self.assertIn("read_file", gemini_md)

    def test_bins_include_toolchains_and_recipes(self) -> None:
        bins = resolve_binaries(workspace_root=self.workspace, refresh=True)
        self.assertIn("ctx_status", bins["one_shot_recipes"])
        self.assertIn(".venv/bin/python", bins["toolchains"]["python"]["sandbox_runners"])
        self.assertFalse(bins["toolchains"]["python"]["sandbox_runners"][".venv/bin/python"]["available"])

    def test_generated_markdown_mentions_one_shot(self) -> None:
        sync_rules(workspace_root=self.workspace)
        text = (self.workspace / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(".venv/bin/ctx status", text)
        self.assertIn("ctx_status", text)
        self.assertIn("`defer`", text)


if __name__ == "__main__":
    unittest.main()
