"""Testes unitários para o motor de RAG local FTS5 e chunkers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ctx.rag import (
    chunk_file,
    extract_markdown_chunks,
    extract_python_chunks,
    get_db,
    get_map,
    index_workspace,
    sanitize_query,
    search,
    search_many,
    status,
)


class TestRAG(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.ctx_dir = self.workspace / ".ctx"
        self.ctx_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.ctx_dir / "index.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_extract_python_chunks(self) -> None:
        code = '''"""Module doc."""

class UserService:
    """Handles users."""
    def get_user(self, user_id: int):
        return {"id": user_id}

def calculate_metrics(data: list) -> int:
    return len(data)
'''
        f = self.workspace / "service.py"
        f.write_text(code, encoding="utf-8")
        chunks = extract_python_chunks(f, "service.py", code)

        titles = [c.title for c in chunks]
        self.assertIn("module docstring: service.py", titles)
        self.assertIn("class UserService", titles)
        self.assertIn("function calculate_metrics", titles)

    def test_extract_markdown_chunks(self) -> None:
        md = """# Arquitetura Principal

Visão geral da solução.

## Componente A
Detalhes do componente A.

### Sub-regra A1
Especificação técnica.
"""
        f = self.workspace / "doc.md"
        f.write_text(md, encoding="utf-8")
        chunks = extract_markdown_chunks(f, "doc.md", md)

        titles = [c.title for c in chunks]
        self.assertIn("Arquitetura Principal", titles)
        self.assertIn("Componente A", titles)
        self.assertIn("Sub-regra A1", titles)

    def test_markdown_ignores_headers_inside_code_fences(self) -> None:
        md = "# Guia\n\n```toml\n# comentário toml\n[x]\n```\n\n## Seção Real\ntexto\n"
        f = self.workspace / "g.md"
        titles = [c.title for c in extract_markdown_chunks(f, "g.md", md)]
        self.assertEqual(titles, ["Guia", "Seção Real"])

    def test_sanitize_query(self) -> None:
        q = "guardrail; rm -rf / 'teste' \"palavra\""
        sanitized = sanitize_query(q)
        self.assertEqual(sanitized, '"guardrail" "rm" "rf" "teste" "palavra"')

    def test_indexing_and_search(self) -> None:
        py_file = self.workspace / "example.py"
        py_file.write_text("def authenticate_agent():\n    return 'auth_token_42'\n", encoding="utf-8")

        md_file = self.workspace / "rules.md"
        md_file.write_text("# Governança de Tokens\n\nEconomia de tokens e alta performance.\n", encoding="utf-8")

        res_idx = index_workspace(workspace_root=self.workspace, force=True)
        self.assertEqual(res_idx["indexed"], 2)

        # Busca por termo existente
        res_search = search(query="authenticate_agent", workspace_root=self.workspace)
        self.assertGreaterEqual(res_search["count"], 1)
        first = res_search["results"][0]
        self.assertEqual(first["path"], "example.py")
        self.assertEqual(first["title"], "function authenticate_agent")

        # Teste do mapa estrutural
        res_map = get_map(workspace_root=self.workspace)
        self.assertIn("example.py", res_map["tree"])
        self.assertIn("rules.md", res_map["tree"])

    def test_index_is_isolated_per_workspace(self) -> None:
        (self.workspace / "a.py").write_text("def isolated_fn():\n    pass\n", encoding="utf-8")
        index_workspace(workspace_root=self.workspace, force=True)
        self.assertTrue(self.db_path.exists())
        con = get_db(self.db_path)
        paths = {r[0] for r in con.execute("SELECT path FROM indexed_files")}
        con.close()
        self.assertEqual(paths, {"a.py"})

    def test_egg_info_is_ignored(self) -> None:
        egg = self.workspace / "src" / "pkg.egg-info"
        egg.mkdir(parents=True)
        (egg / "PKG-INFO").write_text("Name: pkg\n", encoding="utf-8")
        (self.workspace / "src" / "mod.py").write_text("def f():\n    pass\n", encoding="utf-8")
        res_map = get_map(workspace_root=self.workspace)
        self.assertEqual(list(res_map["tree"]), ["src/mod.py"])

    def test_search_many_with_path_and_preview(self) -> None:
        body = "\n".join(f"    x{i} = {i}" for i in range(40))
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "core.py").write_text(f"def alpha_engine():\n{body}\n", encoding="utf-8")
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "notes.md").write_text("# Alpha engine notes\n\nalpha_engine docs.\n", encoding="utf-8")
        (self.workspace / "src" / "beta.py").write_text("def beta_router():\n    return 1\n", encoding="utf-8")

        res = search_many(["alpha_engine", "beta_router"], workspace_root=self.workspace, lines=25)
        self.assertEqual([q["query"] for q in res["queries"]], ["alpha_engine", "beta_router"])
        self.assertGreaterEqual(res["queries"][0]["count"], 1)
        self.assertEqual(res["queries"][1]["results"][0]["path"], "src/beta.py")

        top = next(r for r in res["queries"][0]["results"] if r["path"] == "src/core.py")
        preview_lines = top["preview"].splitlines()
        self.assertEqual(len(preview_lines), 25)
        self.assertIn("def alpha_engine", preview_lines[0])

        scoped = search_many(["alpha_engine"], workspace_root=self.workspace, path_prefix="src")
        self.assertTrue(all(r["path"].startswith("src/") for r in scoped["queries"][0]["results"]))

        no_preview = search("beta_router", workspace_root=self.workspace, lines=0)
        self.assertEqual(no_preview["results"][0]["preview"], "")

    def test_map_subpaths_archive_and_priority(self) -> None:
        files = {
            "README.md": "# Readme\n",
            "pyproject.toml": "[project]\nname='x'\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "src/app.py": "def app():\n    pass\n",
            "openspec/changes/add-x/tasks.md": "# Tasks\n- [x] one\n- [ ] two\n",
            "openspec/changes/archive/old/tasks.md": "# Old\n- [x] done\n",
        }
        for rel, text in files.items():
            p = self.workspace / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

        res = get_map(workspace_root=self.workspace)
        order = list(res["tree"])
        self.assertNotIn("openspec/changes/archive/old/tasks.md", order)
        self.assertEqual(res["hidden_archive_files"], 1)
        self.assertEqual(order[:3], ["src/app.py", "tests/test_a.py", "openspec/changes/add-x/tasks.md"])
        self.assertEqual(order[-1], "README.md")

        with_archive = get_map(workspace_root=self.workspace, include_archive=True)
        self.assertIn("openspec/changes/archive/old/tasks.md", with_archive["tree"])

        scoped = get_map(workspace_root=self.workspace, subpaths=["src", "./tests/"])
        self.assertEqual(list(scoped["tree"]), ["src/app.py", "tests/test_a.py"])

    def test_status_openspec_progress(self) -> None:
        tasks = self.workspace / "openspec" / "changes" / "add-x" / "tasks.md"
        tasks.parent.mkdir(parents=True)
        tasks.write_text("- [x] a\n- [ ] b\n* [X] c\n- [ ] d\n", encoding="utf-8")
        (self.workspace / "openspec" / "changes" / "archive" / "old").mkdir(parents=True)

        res = status(workspace_root=self.workspace)
        spec = res["openspec"]
        self.assertTrue(spec["present"])
        self.assertEqual(spec["archived_count"], 1)
        self.assertEqual(spec["active"][0]["name"], "add-x")
        self.assertEqual((spec["active"][0]["done"], spec["active"][0]["total"]), (2, 4))
        self.assertEqual(spec["active"][0]["next"], ["b", "d"])
        self.assertFalse(res["bins"]["present"])
        self.assertGreaterEqual(res["index"]["files"], 1)


if __name__ == "__main__":
    unittest.main()
