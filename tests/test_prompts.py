"""Testes do catálogo e da instalação de prompts individuais (`ctx prompt`)."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from ctx import __version__
from ctx.policy import sync_rules
from ctx.prompts import (
    CATALOG_PATH,
    KIT_MARKER,
    add_prompts,
    build_catalog,
    extract_snippet,
    list_prompts,
    parse_frontmatter,
    read_source,
    remove_prompts,
    render_catalog,
    update_readme,
)

REPO = Path(__file__).resolve().parent.parent
SOURCE = str(REPO)
GRAPH = "graph-engineering/dag-fanout-fanin"
CONTAINER = "governance-and-guardrails/container-first-e-btrfs"
FILE_TOOLS = "tooling/ferramentas-nativas-de-arquivo"


class TestCatalog(unittest.TestCase):
    def test_catalog_file_in_sync(self) -> None:
        self.assertEqual(
            (REPO / CATALOG_PATH).read_text(encoding="utf-8"),
            render_catalog(REPO),
            "prompts/catalog.json desatualizado: rode `ctx prompt catalog`",
        )

    def test_readme_generated_sections_in_sync(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertEqual(
            readme,
            update_readme(readme, build_catalog(REPO)),
            "Seções geradas do README desatualizadas: rode `ctx prompt catalog`",
        )
        for entry in build_catalog(REPO)["prompts"]:
            self.assertIn(f"`{entry['id']}`", readme)

    def test_update_readme_requires_markers(self) -> None:
        with self.assertRaises(ValueError):
            update_readme("# README sem marcadores\n", build_catalog(REPO))

    def test_docs_reference_current_version(self) -> None:
        expected = f"v{__version__}"
        for rel in ("README.md", "BOOTSTRAP.md", "llms.txt", "prompts/INSTALL.md", "guides/guia-replicacao-rag-e-seguranca.md"):
            text = (REPO / rel).read_text(encoding="utf-8")
            tags = set(re.findall(r"agent-harness-hub(?:/|@)(v\d+\.\d+\.\d+)", text))
            self.assertTrue(tags <= {expected}, f"{rel} cita {tags - {expected}}; esperado só {expected}")

    def test_every_prompt_is_valid(self) -> None:
        catalog = build_catalog(REPO)
        files = sorted(p for p in (REPO / "prompts").rglob("*.md") if p.name != "INSTALL.md")
        self.assertEqual(len(catalog["prompts"]), len(files))
        for entry in catalog["prompts"]:
            self.assertTrue(entry["title"] and entry["summary"], entry["id"])

    def test_template_is_valid_prompt(self) -> None:
        meta, body = parse_frontmatter((REPO / "templates" / "prompt.md.example").read_text(encoding="utf-8"))
        self.assertIn("title", meta)
        self.assertTrue(extract_snippet(body).startswith("### "))

    def test_snippet_extraction(self) -> None:
        body = "# T\n\n## Snippet de Prompt\n\n```markdown\n### Regra\n- a\n```\n\n## Outro\n"
        self.assertEqual(extract_snippet(body), "### Regra\n- a")
        with self.assertRaises(ValueError):
            extract_snippet("# Sem snippet\n")

    def test_only_https_or_local(self) -> None:
        with self.assertRaises(ValueError):
            read_source("http://example.com/", CATALOG_PATH)
        self.assertTrue(list_prompts(SOURCE))


class TestInstall(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, *ids: str, **kw):
        return add_prompts(list(ids), self.ws, source=SOURCE, **kw)

    def test_creates_agents_md_when_no_instruction_file(self) -> None:
        report = self.add(GRAPH)
        self.assertEqual([(r["file"], r["status"]) for r in report], [("AGENTS.md", "adicionado")])
        text = (self.ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(f"agent-harness-hub:prompt {GRAPH} v", text)
        self.assertIn("### Paralelismo Seguro e Graph Engineering", text)

    def test_targets_existing_files_and_is_idempotent(self) -> None:
        (self.ws / "CLAUDE.md").write_text("# Regras do time\n", encoding="utf-8")
        (self.ws / "GEMINI.md").write_text("# Gemini\n", encoding="utf-8")
        first = self.add(GRAPH, CONTAINER)
        self.assertEqual({r["file"] for r in first}, {"CLAUDE.md", "GEMINI.md"})
        self.assertFalse((self.ws / "AGENTS.md").exists())

        snapshot = (self.ws / "CLAUDE.md").read_text(encoding="utf-8")
        second = self.add(GRAPH, CONTAINER)
        self.assertTrue(all(r["status"] == "inalterado" for r in second))
        self.assertEqual((self.ws / "CLAUDE.md").read_text(encoding="utf-8"), snapshot)
        self.assertTrue(snapshot.startswith("# Regras do time\n"))

    def test_updates_stale_block(self) -> None:
        self.add(GRAPH)
        path = self.ws / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8").replace("Graph Engineering", "Versão Antiga", 1), encoding="utf-8")
        report = self.add(GRAPH)
        self.assertEqual(report[0]["status"], "atualizado")
        self.assertEqual(path.read_text(encoding="utf-8").count("START -->"), 1)

    def test_skips_prompt_covered_by_kit(self) -> None:
        (self.ws / "CLAUDE.md").write_text(f"{KIT_MARKER}\nregras\n<!-- AUTOGENERATED_BY_CTX_END -->\n", encoding="utf-8")
        self.assertTrue(self.add(FILE_TOOLS)[0]["status"].startswith("pulado"))
        self.assertEqual(self.add(FILE_TOOLS, force=True)[0]["status"], "adicionado")
        # Gestão de execução não faz parte do kit: o prompt de Graph Engineering instala normalmente
        self.assertEqual(self.add(GRAPH)[0]["status"], "adicionado")

    def test_requires_kit(self) -> None:
        discovery = "discovery/descoberta-em-1-turno"
        self.assertTrue(self.add(discovery)[0]["status"].startswith("pulado (requer o kit"))
        self.assertFalse((self.ws / "AGENTS.md").exists())
        self.assertEqual(self.add(discovery, force=True)[0]["status"], "adicionado")

    def test_explicit_file_and_unknown_id(self) -> None:
        report = self.add(GRAPH, "nao/existe", files=["docs/AI.md"])
        self.assertEqual((report[0]["file"], report[0]["status"]), ("docs/AI.md", "adicionado"))
        self.assertTrue((self.ws / "docs" / "AI.md").exists())
        statuses = {r["id"]: r["status"] for r in report}
        self.assertTrue(statuses["nao/existe"].startswith("não encontrado"))

    def test_remove_only_the_block(self) -> None:
        (self.ws / "AGENTS.md").write_text("# Meu projeto\n\nRegra local.\n", encoding="utf-8")
        self.add(GRAPH, CONTAINER)
        report = remove_prompts([GRAPH], self.ws)
        self.assertIn({"id": GRAPH, "file": "AGENTS.md", "status": "removido"}, report)
        text = (self.ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn(GRAPH, text)
        self.assertIn(CONTAINER, text)
        self.assertIn("Regra local.", text)

    def test_kit_sync_preserves_prompt_blocks(self) -> None:
        (self.ws / "policy.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self.add(GRAPH, force=True)
        sync_rules(workspace_root=self.ws)
        sync_rules(workspace_root=self.ws)
        text = (self.ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(f"agent-harness-hub:prompt {GRAPH}"), 2)  # START + END
        self.assertEqual(text.count(KIT_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
