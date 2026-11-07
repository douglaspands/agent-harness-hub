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


if __name__ == "__main__":
    unittest.main()
