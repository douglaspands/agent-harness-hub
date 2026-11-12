"""Atualização de projetos que têm a versão anterior do kit (`ctx upgrade`): idempotente e não destrutiva."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ctx import __version__, installer
from ctx.installer import init_project, kit_text, render_skill, upgrade_skill_text
from ctx.metrics import guardrail_module
from ctx.policy_upgrade import SECTIONS, SECTION_MARKER, missing_sections, upgrade_policy, upgraded_text
from ctx.rag import db_path_for, index_state, wal_bytes

OLD_POLICY = """# minha anotação importante
[project]
name = "legado"

[ctx]
runner = ".venv/bin/ctx"

[commands]
allow = ["git status", "make test"]
ask = ["git push"]

[harnesses]
enabled = ["claude", "gemini"]

[paths]
deny = [".env"]
"""

OLD_SKILL_CUSTOM = """---
name: workspace-ctx
description: skill antiga
---

# Minha skill

Regra do time: sempre rodar `make lint` antes de commitar.
"""

THIRD_PARTY_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo terceiros"}]}


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".ctx" not in p.parts
    }


class V03Project(unittest.TestCase):
    """Projeto "v0.3": policy sem seções novas, skill sem marcadores, hooks legados e índice de esquema antigo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def zebra():\n    return 1\n", encoding="utf-8")
        (self.root / "policy.toml").write_text(OLD_POLICY, encoding="utf-8")
        for rel in installer.SKILL_TARGETS:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(OLD_SKILL_CUSTOM, encoding="utf-8")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "guardrail.py").write_text("# guardrail v0.3\n", encoding="utf-8")
        claude = self.root / ".claude" / "settings.json"
        claude.parent.mkdir(parents=True, exist_ok=True)
        claude.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(make:*)"]},
                    "hooks": {
                        "PreToolUse": [
                            {"command": "scripts/venv-python scripts/guardrail.py"},  # formato legado inválido
                            THIRD_PARTY_HOOK,
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        # índice no esquema v0.3 + WAL inchado deixado para trás
        db = db_path_for(self.root)
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE indexed_files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, hash TEXT)")
        con.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(path, title, chunk_type, start_line UNINDEXED, end_line UNINDEXED, content)"
        )
        con.commit()
        con.close()
        with open(db.with_name(db.name + "-wal"), "wb") as f:
            f.truncate(100 * 1024 * 1024)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestUpgradeFromPreviousVersion(V03Project):
    def test_upgrade_migrates_everything_and_keeps_customizations(self) -> None:
        res = init_project(self.root)
        status = {a["path"]: a["status"] for a in res["actions"]}

        # policy: conteúdo do usuário intacto (prefixo idêntico) + seções novas acrescentadas
        policy_text = (self.root / "policy.toml").read_text(encoding="utf-8")
        self.assertTrue(policy_text.startswith(OLD_POLICY))
        parsed = tomllib.loads(policy_text)
        self.assertEqual(parsed["commands"]["allow"], ["git status", "make test"])
        for name in SECTIONS:
            self.assertIn(SECTION_MARKER + name, policy_text)
        self.assertEqual(set(res["policy"]["added"]), set(SECTIONS))

        # skill customizada: texto do time preservado + bloco gerenciado inserido
        skill = (self.root / installer.SKILL_TARGETS[0]).read_text(encoding="utf-8")
        self.assertIn("Regra do time: sempre rodar `make lint`", skill)
        self.assertIn(installer.SKILL_START, skill)
        self.assertIn("ctx recall", skill)
        self.assertTrue(status[installer.SKILL_TARGETS[0]].startswith("migrado"))

        # guardrail substituído pela versão do kit; comandos de prompt instalados para os harnesses habilitados
        self.assertEqual((self.root / "scripts" / "guardrail.py").read_text(encoding="utf-8"), kit_text("guardrail.py"))
        self.assertTrue((self.root / ".claude" / "commands" / "ctx-avaliar.md").exists())
        self.assertTrue((self.root / ".gemini" / "commands" / "ctx-avaliar.toml").exists())

        # hooks: entrada legada trocada pela válida; hook de terceiros e permissões preservados
        settings = json.loads((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"allow": ["Bash(make:*)"]})
        entries = settings["hooks"]["PreToolUse"]
        self.assertIn(THIRD_PARTY_HOOK, entries)
        self.assertEqual(sum("guardrail.py" in json.dumps(e) for e in entries), 1)
        self.assertTrue(all("hooks" in e for e in entries))

        # índice: esquema migrado, WAL consolidado, busca funcionando, doctor saudável
        db = db_path_for(self.root)
        self.assertEqual(index_state(db), "ok")
        self.assertLess(wal_bytes(db), 1024 * 1024)
        self.assertTrue(res["doctor"]["all_passed"], [c for c in res["doctor"]["checks"] if not c["passed"]])

    def test_second_run_changes_nothing(self) -> None:
        init_project(self.root)
        before = snapshot(self.root)
        again = init_project(self.root)
        self.assertEqual(snapshot(self.root), before)
        for action in again["actions"]:
            self.assertFalse(action["status"].startswith(("atualizado", "criado", "migrado")), action)
        self.assertEqual(again["policy"]["added"], [])

    def test_dry_run_writes_nothing(self) -> None:
        before = snapshot(self.root)
        wal_before = wal_bytes(db_path_for(self.root))
        res = init_project(self.root, dry_run=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(wal_bytes(db_path_for(self.root)), wal_before)
        self.assertTrue(res["dry_run"])
        self.assertTrue(any(a["status"].startswith("atualizaria") for a in res["actions"]))
        self.assertIn("ctx:section index", res["policy"]["diff"])
        self.assertNotIn("doctor", res)

    def test_runner_comes_from_existing_policy(self) -> None:
        (self.root / "policy.toml").write_text(OLD_POLICY.replace(".venv/bin/ctx", "ctx"), encoding="utf-8")
        res = init_project(self.root)
        self.assertEqual(res["runner"], "ctx")
        self.assertIn("`ctx recall", (self.root / installer.SKILL_TARGETS[0]).read_text(encoding="utf-8"))

    def test_gitignore_equivalent_entries_are_not_duplicated(self) -> None:
        (self.root / ".gitignore").write_text(".venv\n/.ctx\n", encoding="utf-8")
        init_project(self.root)
        lines = (self.root / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertNotIn(".venv/", lines)
        self.assertNotIn(".ctx/", lines)
        self.assertIn(".claude/settings.local.json", lines)

    def test_gemini_command_only_when_harness_enabled(self) -> None:
        (self.root / "policy.toml").write_text(OLD_POLICY.replace('["claude", "gemini"]', '["claude"]'), encoding="utf-8")
        init_project(self.root)
        self.assertTrue((self.root / ".claude" / "commands" / "ctx-avaliar.md").exists())
        self.assertFalse((self.root / ".gemini" / "commands" / "ctx-avaliar.toml").exists())


class TestSkillMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.new = render_skill(".venv/bin/ctx")

    def test_identical_is_unchanged(self) -> None:
        self.assertEqual(upgrade_skill_text(self.new, self.new), (self.new, "inalterado"))

    def test_managed_block_is_replaced_and_user_text_kept(self) -> None:
        a = self.new.index(installer.SKILL_START)
        old = self.new[:a] + f"{installer.SKILL_START} antigo -->\ntexto velho\n{installer.SKILL_END}\n\n## Extra do time\nnão apagar\n"
        merged, status = upgrade_skill_text(old, self.new)
        self.assertEqual(status, "atualizado (bloco gerenciado)")
        self.assertIn("## Extra do time\nnão apagar", merged)
        self.assertNotIn("texto velho", merged)
        self.assertIn("ctx recall", merged)
        self.assertEqual(upgrade_skill_text(merged, self.new)[1], "inalterado")  # idempotente

    def test_published_legacy_version_is_replaced_whole(self) -> None:
        legacy = "---\nname: workspace-ctx\n---\n# versão publicada antes do bloco gerenciado\n"
        digest = hashlib.sha256(legacy.encode()).hexdigest()
        with mock.patch.object(installer, "LEGACY_SKILL_SHA256", {digest}):
            merged, status = upgrade_skill_text(legacy, self.new)
        self.assertEqual(merged, self.new)
        self.assertIn("versão anterior do kit", status)

    def test_unknown_runner_variant_is_treated_as_customized(self) -> None:
        """Sem como provar que é o texto do kit, preserva o conteúdo (nunca perde dados)."""
        legacy = "---\nname: workspace-ctx\n---\nuse meu-runner status\n"
        merged, status = upgrade_skill_text(legacy, self.new)
        self.assertTrue(status.startswith("migrado"))
        self.assertIn("use meu-runner status", merged)

    def test_customized_without_markers_is_preserved(self) -> None:
        merged, status = upgrade_skill_text(OLD_SKILL_CUSTOM, self.new)
        self.assertTrue(status.startswith("migrado"))
        self.assertTrue(merged.startswith("---\nname: workspace-ctx\ndescription: skill antiga\n---\n"))
        self.assertIn("Regra do time", merged)
        self.assertIn(installer.SKILL_START, merged)
        self.assertEqual(upgrade_skill_text(merged, self.new)[1], "inalterado")

    def test_without_frontmatter(self) -> None:
        merged, _status = upgrade_skill_text("# só texto do time\n", self.new)
        self.assertTrue(merged.startswith("---\n"))
        self.assertIn("só texto do time", merged)

    def test_real_historic_versions_are_registered(self) -> None:
        self.assertEqual(len(installer.LEGACY_SKILL_SHA256), 3)  # v0.1/v0.2, v0.3 e v0.3 com runner `ctx`


class TestPolicyMerge(unittest.TestCase):
    def test_idempotent_and_appends_only_missing(self) -> None:
        text = OLD_POLICY + '\n[search]\ntimeout_s = 9.0\n'
        new, added = upgraded_text(text, __version__)
        self.assertNotIn("search", added)  # tabela do usuário é respeitada
        self.assertTrue(new.startswith(text))
        again, added2 = upgraded_text(new, __version__)
        self.assertEqual((again, added2), (new, []))

    def test_appended_text_is_valid_toml_and_changes_nothing(self) -> None:
        new, _ = upgraded_text(OLD_POLICY, __version__)
        self.assertEqual(tomllib.loads(new), tomllib.loads(OLD_POLICY))  # só comentários

    def test_handles_missing_trailing_newline_and_empty_file(self) -> None:
        for text in ("[a]\nx = 1", ""):
            new, added = upgraded_text(text, __version__)
            tomllib.loads(new)
            self.assertEqual(set(added), set(SECTIONS))

    def test_missing_policy_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(upgrade_policy(Path(d), __version__)["status"], "ausente")

    def test_templates_contain_every_section(self) -> None:
        template = kit_text("policy.toml.example")
        self.assertEqual(missing_sections(template), [])
        for _name, (_table, block) in SECTIONS.items():
            self.assertIn(block.rstrip("\n"), template)


class TestCompatibilityAcrossVersions(unittest.TestCase):
    """Guardrail novo + policy antiga e guardrail com policy nova (chaves desconhecidas) decidem igual."""

    def test_unknown_sections_do_not_change_decisions(self) -> None:
        g = guardrail_module()
        old = tomllib.loads(OLD_POLICY)
        new = tomllib.loads(upgraded_text(OLD_POLICY, __version__)[0] + '\n[recipes.x]\nsteps = ["ls"]\n[index]\nmax_file_kb = 1\n')
        for cmd in ("git status", "make test", "git push", "cat .env", "ls -R"):
            self.assertEqual(g.evaluate_shell_command(cmd, old)[0], g.evaluate_shell_command(cmd, new)[0], cmd)


if __name__ == "__main__":
    unittest.main()
