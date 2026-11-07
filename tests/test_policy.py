"""Testes unitários para o sincronizador de políticas, cache de binários e diagnóstico."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ctx.policy import doctor, load_policy, resolve_binaries, sync_rules


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

[toolchains.python]
canonical_runner = "uv run"
forbidden_direct = ["python3"]
""",
            encoding="utf-8",
        )

        # Mock de guardrail
        scripts_dir = self.workspace / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "guardrail.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

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
        self.assertTrue(doc_res["all_passed"])
        self.assertEqual(doc_res["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
