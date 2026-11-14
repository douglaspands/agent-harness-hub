"""Saúde do índice: checkpoint do WAL, busca somente leitura, timeout, migração de esquema, doctor e --fix."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ctx import rag
from ctx.policy import doctor
from ctx.rag import db_path_for, index_info, index_state, index_workspace, search_many, wal_bytes

MB = 1024 * 1024


def populate(root: Path, n: int = 30) -> None:
    for i in range(n):
        p = root / "src" / f"mod_{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'"""Modulo {i}."""\n\ndef funcao_{i}(x):\n    return x + {i}  # zebra\n', encoding="utf-8")


def fake_bloated_wal(db: Path, size: int = 100 * MB) -> Path:
    wal = db.with_name(db.name + "-wal")
    with open(wal, "wb") as f:
        f.truncate(size)  # arquivo esparso: só o tamanho importa para o doctor
    return wal


class IndexTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        populate(self.root)
        self.db = db_path_for(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestCheckpoint(IndexTestBase):
    def test_index_leaves_no_wal_behind(self) -> None:
        res = index_workspace(self.root)
        self.assertGreater(res["indexed"], 0)
        self.assertEqual(wal_bytes(self.db), 0)
        self.assertEqual(res["wal_bytes"], 0)

    def test_checkpoint_truncates_pending_wal(self) -> None:
        index_workspace(self.root)
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("INSERT INTO meta (key, value) VALUES ('x', ?)", ("y" * 200000,))
        con.commit()
        self.assertGreater(wal_bytes(self.db), 0)  # WAL pendente enquanto a conexão está aberta
        res = rag.checkpoint(self.db)
        con.close()
        self.assertGreater(res["before"], 0)
        self.assertEqual(wal_bytes(self.db), 0)

    def test_pragmas_applied(self) -> None:
        con = rag.get_db(self.db, {"journal_size_limit_mb": 3, "wal_autocheckpoint_pages": 77})
        try:
            self.assertEqual(con.execute("PRAGMA journal_size_limit").fetchone()[0], 3 * MB)
            self.assertEqual(con.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 77)
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            con.close()


class TestReadOnlySearch(IndexTestBase):
    def test_search_does_not_write_the_index(self) -> None:
        index_workspace(self.root)
        before = (self.db.stat().st_mtime_ns, self.db.stat().st_size, wal_bytes(self.db))
        for _ in range(3):
            res = search_many(["zebra", "funcao_3"], workspace_root=self.root)
            self.assertTrue(res["queries"][0]["results"])
        after = (self.db.stat().st_mtime_ns, self.db.stat().st_size, wal_bytes(self.db))
        self.assertEqual(before, after)

    def test_search_does_not_reindex_changed_files_but_warns(self) -> None:
        index_workspace(self.root)
        time.sleep(0.01)
        (self.root / "src" / "mod_1.py").write_text("def funcao_1():\n    return 'zebra novo e maior'\n", encoding="utf-8")
        res = search_many(["zebra"], workspace_root=self.root, top_k=50)
        self.assertTrue(any("mudaram desde a indexação" in w for w in res["warnings"]), res["warnings"])
        self.assertFalse(any("novo e maior" in r["snippet"] for r in res["queries"][0]["results"]))

    def test_incremental_refresh_is_opt_in(self) -> None:
        (self.root / "policy.toml").write_text('[search]\nrefresh = "incremental"\n', encoding="utf-8")
        index_workspace(self.root)
        time.sleep(0.01)
        (self.root / "src" / "mod_2.py").write_text("def novo():\n    return 'unicornio'\n", encoding="utf-8")
        res = search_many(["unicornio"], workspace_root=self.root)
        self.assertTrue(res["queries"][0]["results"])

    def test_healthy_index_is_fast(self) -> None:
        populate(self.root, 400)
        index_workspace(self.root)
        search_many(["zebra"], workspace_root=self.root)  # aquece
        res = search_many(["zebra", "funcao_7", "modulo"], workspace_root=self.root, lines=0)
        self.assertLess(res["elapsed_ms"], 50)


class TestTimeout(IndexTestBase):
    def test_zero_timeout_returns_rg_equivalent(self) -> None:
        index_workspace(self.root)
        res = search_many(["funcao zebra"], workspace_root=self.root, timeout=0, path_prefix="src")
        q = res["queries"][0]
        self.assertTrue(res["timed_out"])
        self.assertTrue(q["timed_out"])
        self.assertEqual(q["rg"], "rg -n --heading -C 2 -e funcao -e zebra src")

    def test_progress_handler_interrupts_slow_query(self) -> None:
        index_workspace(self.root)
        con = rag.open_readonly(self.db)
        try:
            deadline = time.perf_counter() + 0.02
            # a query "demora" 30 ms por passo da VM e é interrompida pelo handler, como no search_many real
            con.set_progress_handler(lambda: (time.sleep(0.03), 1 if time.perf_counter() > deadline else 0)[1], 1)
            out = rag._search_one(con, self.root, "zebra", 5, None, 0, {}, None, deadline)
        finally:
            con.close()
        self.assertTrue(out["timed_out"])

    def test_never_exceeds_timeout(self) -> None:
        populate(self.root, 200)
        index_workspace(self.root)
        t0 = time.perf_counter()
        search_many(["zebra"] * 8, workspace_root=self.root, timeout=0.5)
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestMigrationAndRepair(IndexTestBase):
    def make_v03_index(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE indexed_files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, hash TEXT)")
        con.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(path, title, chunk_type, start_line UNINDEXED, end_line UNINDEXED, content)"
        )
        con.execute("INSERT INTO chunks VALUES ('velho.py', 't', 'x', 1, 2, 'conteudo antigo')")
        con.commit()
        con.close()

    def test_previous_version_index_is_migrated_transparently(self) -> None:
        self.make_v03_index()
        self.assertEqual(index_state(self.db), "outdated")
        res = search_many(["zebra"], workspace_root=self.root)
        self.assertTrue(res["queries"][0]["results"])
        self.assertEqual(index_state(self.db), "ok")
        self.assertTrue(any("recriado" in w for w in res["warnings"]))
        old = search_many(["antigo"], workspace_root=self.root)
        self.assertEqual(old["queries"][0]["count"], 0)

    def test_v04_index_is_rebuilt_with_trigrams_and_name(self) -> None:
        """Índice da 0.4 (esquema 2): a primeira busca recria com `name`, `chunks_tri` e `vectors`, sem intervenção."""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE indexed_files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, hash TEXT, kind TEXT)")
        con.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(path, title, chunk_type, start_line UNINDEXED, end_line UNINDEXED, "
            "content, kind UNINDEXED)"
        )
        con.execute("PRAGMA user_version=2")
        con.commit()
        con.close()
        self.assertEqual(index_state(self.db), "outdated")
        res = search_many(["zebra"], workspace_root=self.root)
        self.assertTrue(res["queries"][0]["results"])
        con = sqlite3.connect(self.db)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        cols = [r[1] for r in con.execute("PRAGMA table_info(chunks)")]
        con.close()
        self.assertTrue({"chunks_tri", "vectors"} <= names)
        self.assertIn("name", cols)

    def test_corrupt_database_is_recreated(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.db.write_bytes(b"isto nao e um banco sqlite" * 100)
        res = search_many(["zebra"], workspace_root=self.root)
        self.assertTrue(res["queries"][0]["results"])
        self.assertEqual(index_state(self.db), "ok")

    def test_huge_wal_is_discarded_on_search(self) -> None:
        index_workspace(self.root)
        fake_bloated_wal(self.db, 3 * MB)
        with mock.patch.object(rag, "WAL_REBUILD_BYTES", 1 * MB):
            res = search_many(["zebra"], workspace_root=self.root)
        self.assertTrue(res["queries"][0]["results"])
        self.assertTrue(any("WAL" in w for w in res["warnings"]))
        self.assertLess(wal_bytes(self.db), 1 * MB)


class TestDoctor(IndexTestBase):
    def checks(self, **kw: bool) -> dict[str, dict]:
        return {c["name"]: c for c in doctor(self.root, **kw)["checks"]}

    def test_healthy_index_passes_index_checks(self) -> None:
        index_workspace(self.root)
        checks = {n: c for n, c in self.checks().items() if n.startswith("Índice")}
        self.assertTrue(checks)
        self.assertTrue(all(c["passed"] for c in checks.values()), checks)

    def test_detects_bloated_wal_and_fix_repairs_it(self) -> None:
        index_workspace(self.root)
        fake_bloated_wal(self.db, 100 * MB)
        bad = self.checks()["Índice: WAL consolidado"]
        self.assertFalse(bad["passed"])
        self.assertIn("100.0 MB", bad["detail"])

        res = doctor(self.root, fix=True)
        self.assertIn("fix", res)
        self.assertGreater(res["fix"]["before"]["wal"], 64 * MB)
        self.assertLess(res["fix"]["after"]["wal"], 64 * MB)
        self.assertTrue(self.checks()["Índice: WAL consolidado"]["passed"])
        self.assertTrue(search_many(["zebra"], workspace_root=self.root)["queries"][0]["results"])

    def test_detects_db_over_limit(self) -> None:
        index_workspace(self.root)
        (self.root / "policy.toml").write_text("[doctor]\ndb_max_mb = 0\n", encoding="utf-8")
        self.assertFalse(self.checks()["Índice: tamanho do banco"]["passed"])

    def test_detects_index_behind_files(self) -> None:
        index_workspace(self.root)
        self.assertTrue(self.checks()["Índice: em dia com os arquivos"]["passed"])
        (self.root / "src" / "novo.py").write_text("def x():\n    pass\n", encoding="utf-8")
        stale = self.checks()["Índice: em dia com os arquivos"]
        self.assertFalse(stale["passed"])
        self.assertIn("1 arquivo", stale["detail"])
        self.assertTrue(doctor(self.root, fix=True)["fix"])
        self.assertTrue(self.checks()["Índice: em dia com os arquivos"]["passed"])

    def test_slow_search_fails_check(self) -> None:
        index_workspace(self.root)
        (self.root / "policy.toml").write_text("[doctor]\nsearch_max_ms = 0\n", encoding="utf-8")
        self.assertFalse(self.checks()["Índice: busca de teste"]["passed"])

    def test_fix_recovers_corrupt_index(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.db.write_bytes(b"lixo" * 1000)
        res = doctor(self.root, fix=True)
        self.assertEqual(index_state(self.db), "ok")
        self.assertIn("índice recriado", res["fix"]["steps"][0])


class TestStatusInfo(IndexTestBase):
    def test_size_files_age_scope(self) -> None:
        index_workspace(self.root)
        info = index_info(self.root)
        self.assertEqual(info["files"], 30)
        self.assertGreater(info["db_bytes"], 0)
        self.assertIsNotNone(info["age_s"])
        self.assertIn("gitignore", info["scope"])
        self.assertEqual(info["by_kind"], {"code": 30})


if __name__ == "__main__":
    unittest.main()
