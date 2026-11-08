"""Testes do instalador `ctx init` e da sincronia entre o kit empacotado e os arquivos do repositório."""

from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from ctx import find_workspace_root
from ctx.installer import init_project, install_commands, kit_text
from ctx.policy import validate_claude_hooks

REPO = Path(__file__).resolve().parent.parent


class TestKitInSync(unittest.TestCase):
    """O kit (src/ctx/kit) é a fonte do `ctx init`; as cópias do repositório não podem divergir."""

    def test_guardrail(self) -> None:
        self.assertEqual(kit_text("guardrail.py"), (REPO / "scripts" / "guardrail.py").read_text(encoding="utf-8"))

    def test_policy_template(self) -> None:
        self.assertEqual(kit_text("policy.toml.example"), (REPO / "templates" / "policy.toml.example").read_text(encoding="utf-8"))

    def test_venv_python(self) -> None:
        self.assertEqual(kit_text("venv-python"), (REPO / "scripts" / "venv-python").read_text(encoding="utf-8"))

    def test_skills(self) -> None:
        for rel in (".claude/skills/workspace-ctx/SKILL.md", ".agents/skills/workspace-ctx/SKILL.md"):
            self.assertEqual(kit_text("SKILL.md"), (REPO / rel).read_text(encoding="utf-8"), rel)


class TestInit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "meu-app"
        self.target.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_python_project(self) -> None:
        (self.target / "pyproject.toml").write_text("[project]\nname='meu-app'\n", encoding="utf-8")
        res = init_project(self.target)

        self.assertEqual(res["runner"], ".venv/bin/ctx")
        self.assertTrue(res["doctor"]["all_passed"], res["doctor"]["checks"])
        for rel in ("scripts/guardrail.py", "policy.toml", ".claude/skills/workspace-ctx/SKILL.md", ".agents/skills/workspace-ctx/SKILL.md"):
            self.assertTrue((self.target / rel).exists(), rel)
        self.assertTrue((self.target / "scripts" / "guardrail.py").stat().st_mode & 0o111)

        policy = tomllib.loads((self.target / "policy.toml").read_text(encoding="utf-8"))
        self.assertEqual(policy["project"]["name"], "meu-app")
        self.assertEqual(policy["ctx"]["runner"], ".venv/bin/ctx")

        settings = json.loads((self.target / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(validate_claude_hooks(settings)[0])
        self.assertIn("`.venv/bin/ctx status`", (self.target / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertTrue((self.target / "scripts" / "venv-python").stat().st_mode & 0o111)
        self.assertIn(".venv/", (self.target / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_non_python_project_defaults_to_venv_runner(self) -> None:
        (self.target / "package.json").write_text("{}", encoding="utf-8")
        res = init_project(self.target)
        self.assertEqual(res["runner"], ".venv/bin/ctx")
        self.assertTrue(res["doctor"]["all_passed"], res["doctor"]["checks"])

    def test_install_commands_never_use_global_python_beyond_venv(self) -> None:
        (self.target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self.assertEqual(len(install_commands(self.target, has_uv=True)), 1)
        self.assertTrue(install_commands(self.target, has_uv=True)[0].startswith("uv add --dev"))

        (self.target / "pyproject.toml").unlink()
        self.assertEqual(install_commands(self.target, has_uv=True)[0], "uv venv .venv")

        no_uv = install_commands(self.target, has_uv=False)
        self.assertEqual(no_uv[0], "python3 -m venv .venv")
        self.assertTrue(no_uv[1].startswith(".venv/bin/python -m pip install"))
        self.assertTrue(all(not c.startswith("python3") for c in no_uv[1:]))

    def test_explicit_ctx_runner(self) -> None:
        res = init_project(self.target, runner="ctx")

        self.assertEqual(res["runner"], "ctx")
        policy_text = (self.target / "policy.toml").read_text(encoding="utf-8")
        self.assertNotIn(".venv/bin/ctx", policy_text)
        self.assertEqual(tomllib.loads(policy_text)["commands"]["one_shot_recipes"]["ctx_status"], "ctx status")
        self.assertIn("`ctx status`", (self.target / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn(".venv/bin/ctx", (self.target / ".claude/skills/workspace-ctx/SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(any("uv tool install" in s for s in res["next_steps"]))

    def test_idempotent_and_preserves_customizations(self) -> None:
        (self.target / ".gitignore").write_text("node_modules/\n.ctx/", encoding="utf-8")
        (self.target / "CLAUDE.md").write_text("# Regras do time\n\nUse tabs.\n", encoding="utf-8")
        init_project(self.target)

        custom = (self.target / "policy.toml").read_text(encoding="utf-8") + "\n# customizado\n"
        (self.target / "policy.toml").write_text(custom, encoding="utf-8")
        (self.target / "scripts" / "guardrail.py").write_text("# versão antiga\n", encoding="utf-8")

        res = init_project(self.target)
        status = {a["path"]: a["status"] for a in res["actions"]}
        self.assertTrue(status["policy.toml"].startswith("mantido"))
        self.assertEqual(status["scripts/guardrail.py"], "atualizado")
        self.assertEqual(status[".gitignore"], "inalterado")
        self.assertEqual((self.target / "policy.toml").read_text(encoding="utf-8"), custom)

        gitignore = (self.target / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(gitignore.count(".ctx/"), 1)
        self.assertIn(".claude/settings.local.json", gitignore)

        claude_md = (self.target / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Use tabs.", claude_md)
        self.assertEqual(claude_md.count("AUTOGENERATED_BY_CTX_START"), 1)

        forced = init_project(self.target, force=True)
        self.assertEqual({a["path"]: a["status"] for a in forced["actions"]}["policy.toml"], "atualizado")

    def test_no_sync_only_copies(self) -> None:
        res = init_project(self.target, sync=False)
        self.assertNotIn("sync", res)
        self.assertFalse((self.target / ".claude" / "settings.json").exists())
        self.assertTrue((self.target / "policy.toml").exists())


class TestWorkspaceRoot(unittest.TestCase):
    def test_prefers_policy_then_git_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            nested = root / "pkg" / "src"
            nested.mkdir(parents=True)
            self.assertEqual(find_workspace_root(nested), root.resolve())
            (root / "pkg" / "policy.toml").write_text("", encoding="utf-8")
            self.assertEqual(find_workspace_root(nested), (root / "pkg").resolve())


if __name__ == "__main__":
    unittest.main()
