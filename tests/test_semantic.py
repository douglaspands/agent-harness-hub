"""Semântica híbrida opcional (M-05): fusão com o BM25, pulo quando o BM25 basta, busca sem rede, aviso único."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctx import semantic
from ctx.rag import db_path_for, index_workspace, search_many

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".ctx").mkdir()
        (self.root / "glue.c").write_text("int dw_mipi_dsi2_get_lane_mbps(void) { return 1; }\n", encoding="utf-8")
        (self.root / "a.txt").write_text("alpha beta\n", encoding="utf-8")
        (self.root / "b.txt").write_text("alpha gamma\n", encoding="utf-8")
        with mock.patch("ctx.semantic.available", return_value=False):
            index_workspace(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rowid(self, path: str) -> int:
        con = sqlite3.connect(db_path_for(self.root))
        try:
            return int(con.execute("SELECT rowid FROM chunks WHERE path = ?", (path,)).fetchone()[0])
        finally:
            con.close()


class TestHybrid(Base):
    def test_semantic_only_result_is_labelled(self) -> None:
        rid = self.rowid("glue.c")
        with mock.patch("ctx.rag.search_model", return_value=object()), \
                mock.patch("ctx.semantic.top_rowids", return_value=[(rid, 0.9)]):
            q = search_many(["onde calcula a taxa"], workspace_root=self.root)["queries"][0]
        top = q["results"][0]
        self.assertEqual((top["path"], top["via"]), ("glue.c", "sem"))
        self.assertEqual(q["mode"], "semantic")

    def test_both_lists_are_fused(self) -> None:
        rid = self.rowid("b.txt")
        with mock.patch("ctx.rag.search_model", return_value=object()), \
                mock.patch("ctx.semantic.top_rowids", return_value=[(rid, 0.9)]):
            q = search_many(["alpha"], workspace_root=self.root, top_k=5)["queries"][0]
        vias = {r["path"]: r["via"] for r in q["results"]}
        self.assertEqual(vias["b.txt"], "bm25+sem")
        self.assertEqual(q["results"][0]["path"], "b.txt")  # nas duas listas: vence

    def test_semantic_is_skipped_when_bm25_already_fills_top_k(self) -> None:
        with mock.patch("ctx.rag.search_model", return_value=object()), \
                mock.patch("ctx.semantic.top_rowids") as top:
            search_many(["alpha"], workspace_root=self.root, top_k=2)
        top.assert_not_called()


class TestNoNetworkOnSearch(Base):
    def test_search_never_downloads(self) -> None:
        with mock.patch("ctx.semantic.get_model") as get:
            search_many(["alpha"], workspace_root=self.root)
        get.assert_not_called()  # modelo ausente em .ctx/models: nem tenta carregar

    def test_rebuild_triggered_by_search_does_not_download(self) -> None:
        db_path_for(self.root).unlink()
        with mock.patch("ctx.semantic.available", return_value=True), mock.patch("ctx.semantic._download") as dl:
            search_many(["alpha"], workspace_root=self.root)
        dl.assert_not_called()


class TestNotes(unittest.TestCase):
    def test_missing_extra_is_reported_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.txt").write_text("x\n", encoding="utf-8")
            with mock.patch("ctx.semantic.available", return_value=False):
                first = index_workspace(root)["notes"]
                second = index_workspace(root)["notes"]
        self.assertTrue(any("semantic" in n for n in first))
        self.assertFalse(any("semantic" in n for n in second))

    def test_disabled_in_policy_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "policy.toml").write_text("[search]\nsemantic = false\n", encoding="utf-8")
            with mock.patch("ctx.semantic.available", return_value=False):
                notes = index_workspace(root)["notes"]
        self.assertFalse(any("semantic" in n for n in notes))


@unittest.skipUnless(HAS_NUMPY, "numpy não instalado (vem com o extra `semantic`)")
class TestVectors(unittest.TestCase):
    def test_cosine_top_rowids(self) -> None:
        import numpy as np

        class Fake:
            def encode(self, texts: list[str]) -> np.ndarray:
                return np.array([[1.0, 0.0] if "glue" in t else [0.0, 1.0] for t in texts])

        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE vectors (id INTEGER PRIMARY KEY, path TEXT, vec BLOB)")
        vecs = semantic.encode(Fake(), ["glue c", "outro"])
        con.executemany("INSERT INTO vectors VALUES (?, ?, ?)", [(1, "g", semantic.to_blob(vecs[0])), (2, "o", semantic.to_blob(vecs[1]))])
        self.assertEqual(semantic.top_rowids(con, Fake(), "glue", 2)[0][0], 1)


if __name__ == "__main__":
    unittest.main()
