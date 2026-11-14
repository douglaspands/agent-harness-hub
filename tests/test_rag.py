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


class TestSearchQuality(unittest.TestCase):
    """M-01, M-02, M-04, M-06, M-11: recuo da consulta, trigramas, glossário, palavras vazias e prévia."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".ctx").mkdir()
        (self.root / "phy-inno-dsidphy.c").write_text(
            "/* licença */\n" * 40 + "static int inno_dsidphy_init(void)\n{\n\treturn lane_mbps;\n}\n", encoding="utf-8"
        )
        (self.root / "board.dts").write_text("backlight: backlight {\n\tpwms = <&pwm1 0 25000 0>;\n};\n", encoding="utf-8")
        (self.root / "notas.md").write_text("# Painel\nO painel fica cinza sem vídeo.\n", encoding="utf-8")
        (self.root / "dados.jsonl").write_text('{"k": "' + "x" * 500 + ' marcador"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def query(self, q: str, **kw: object) -> dict:
        return search_many([q], workspace_root=self.root, **kw)["queries"][0]  # type: ignore[arg-type]

    def test_all_terms_first(self) -> None:
        q = self.query("backlight pwms")
        self.assertEqual((q["mode"], q["results"][0]["path"]), ("and", "board.dts"))

    def test_substring_of_identifier_falls_back_to_trigrams(self) -> None:
        q = self.query("dsidph")
        self.assertEqual(q["mode"], "trigram")
        self.assertEqual(q["results"][0]["path"], "phy-inno-dsidphy.c")

    def test_natural_language_falls_back_to_any_term(self) -> None:
        q = self.query("factory dtb backlight pwms")
        self.assertEqual(q["mode"], "or")
        self.assertEqual(q["results"][0]["path"], "board.dts")

    def test_stopwords_do_not_break_all_terms(self) -> None:
        self.assertEqual(sanitize_query("onde o painel fica cinza"), '"painel" "cinza"')
        self.assertEqual(sanitize_query("the"), '"the"')  # só palavras vazias: mantém
        self.assertEqual(sanitize_query("x y", "or"), '"x" OR "y"')
        self.assertEqual(sanitize_query("ab dsidph", "trigram"), '"dsidph"')
        self.assertEqual(self.query("o painel com vídeo")["mode"], "and")

    def test_glossary_translates_and_reports(self) -> None:
        (self.root / "policy.toml").write_text('[search]\nglossary = [["luz de fundo", "backlight"]]\n', encoding="utf-8")
        q = self.query("luz de fundo", path_prefix="board.dts")
        self.assertEqual(q["results"][0]["path"], "board.dts")  # "luz de fundo" não aparece no DTS
        self.assertTrue(q["mode"].endswith("+glossary"))
        self.assertEqual(q["glossary"], ["luz de fundo → backlight"])

    def test_preview_centered_on_match_and_long_lines_cut(self) -> None:
        r = self.query("lane_mbps", lines=5)["results"][0]
        first = int(r["preview"].splitlines()[0].split()[0])
        self.assertEqual(first, 40)  # 3 linhas antes da linha 43 (lane_mbps): pulou o cabeçalho de licença
        self.assertIn("lane_mbps", r["preview"])
        line = self.query("marcador", lines=3)["results"][0]["preview"]
        self.assertIn("…(+", line)
        self.assertLess(len(line), 260)


class TestFileName(unittest.TestCase):
    """M-26: o nome do arquivo pesa no ranking; com refresh incremental, resultados desatualizados são reindexados."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".ctx").mkdir()
        pkg = self.root / "src" / "r36os"
        pkg.mkdir(parents=True)
        (pkg / "push.py").write_text(
            '"""Update a console over the USB cable: `r36os push`."""\n\n\ndef run(args):\n    return sync(args)\n',
            encoding="utf-8",
        )
        # concorrentes que repetem "push" e "r36os" no conteúdo e no título
        (pkg / "services.py").write_text(
            '"""r36os services: push, push, push."""\n\n\ndef push_service(r36os):\n    """push the r36os push service."""\n',
            encoding="utf-8",
        )
        (self.root / "notes.md").write_text("# r36os push\n\nr36os push push push over ssh.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_module_named_after_a_query_term_comes_first(self) -> None:
        top = search_many(["r36os push"], workspace_root=self.root)["queries"][0]["results"][0]
        self.assertEqual(top["path"], "src/r36os/push.py")

    def test_stale_results_are_reindexed_with_incremental_refresh(self) -> None:
        from unittest import mock

        index_workspace(self.root)
        (self.root / "notes.md").write_text("# r36os push\n\nzanzibar novo conteúdo.\n", encoding="utf-8")
        quiet = {"reindexed": False, "repaired": False, "warnings": []}
        with mock.patch("ctx.rag.ensure_current", return_value=quiet):
            off = search_many(["r36os push"], workspace_root=self.root)
            self.assertTrue(any("mudaram desde a indexação" in w for w in off["warnings"]))
            (self.root / "policy.toml").write_text('[search]\nrefresh = "incremental"\n', encoding="utf-8")
            res = search_many(["r36os push"], workspace_root=self.root)
        self.assertFalse(any("mudaram desde a indexação" in w for w in res["warnings"]))
        z = search_many(["zanzibar"], workspace_root=self.root)["queries"][0]
        self.assertEqual(z["results"][0]["path"], "notes.md")


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

    def test_long_markdown_section_is_split_keeping_the_title(self) -> None:
        """M-13: seção longa vira partes por item de lista/parágrafo, todas com o título da seção."""
        items = "\n".join(f"- item {i}: texto da tarefa {i}" for i in range(150))
        md = f"# Tarefas\n\n{items}\n\n## Curta\nok\n"
        f = self.workspace / "tasks.md"
        chunks = extract_markdown_chunks(f, "tasks.md", md)
        tarefas = [c for c in chunks if c.title.startswith("Tarefas")]
        self.assertGreaterEqual(len(tarefas), 3)
        self.assertEqual(tarefas[1].title, "Tarefas (parte 2)")
        self.assertTrue(all(c.end_line - c.start_line + 1 <= 60 for c in tarefas))
        self.assertTrue(all(c.content.startswith("- item") for c in tarefas[1:]))  # corta no item, não no meio
        self.assertEqual(sum(c.content.count("- item") for c in tarefas), 150)  # nada se perde
        self.assertEqual(chunks[-1].title, "Curta")

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
