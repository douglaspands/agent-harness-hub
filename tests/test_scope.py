"""Escopo do índice: .gitignore, binários, tamanho, diretórios de build, allowlist de referência, deny e patches."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from ctx.rag import collect_files, db_path_for, extract_patch_chunks, index_workspace, search_many

PATCH = """From 1111 Mon Sep 17 00:00:00 2001
Subject: [PATCH] add audio gain control

diff --git a/drivers/audio.c b/drivers/audio.c
index 111..222 100644
--- a/drivers/audio.c
+++ b/drivers/audio.c
@@ -10,6 +10,12 @@ static int audio_probe(struct device *dev)
 	int ret;
+static int audio_set_gain(struct device *dev, int gain)
+{
+	return 0;
+}
 	return ret;
diff --git a/tools/gain.py b/tools/gain.py
new file mode 100644
--- /dev/null
+++ b/tools/gain.py
@@ -0,0 +1,3 @@
+def apply_gain(x):
+    return x
+class GainTool:
"""


def write(root: Path, rel: str, data: str | bytes = "conteudo\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


class ScopeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rels(self) -> dict[str, str]:
        return {rel: kind for _p, rel, kind in collect_files(self.root)}


class TestExclusions(ScopeBase):
    def test_gitignore_without_git(self) -> None:
        write(self.root, ".gitignore", "secret_dir/\n*.log\n/only_root.txt\n")
        write(self.root, "keep.txt")
        write(self.root, "secret_dir/a.txt")
        write(self.root, "x.log")
        write(self.root, "only_root.txt")
        write(self.root, "sub/only_root.txt")
        rels = self.rels()
        self.assertIn("keep.txt", rels)
        self.assertIn("sub/only_root.txt", rels)
        for gone in ("secret_dir/a.txt", "x.log", "only_root.txt"):
            self.assertNotIn(gone, rels)

    @unittest.skipUnless(shutil.which("git"), "git ausente")
    def test_gitignore_with_git(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        write(self.root, ".gitignore", "ignored.txt\nout/\n")
        write(self.root, "tracked.txt")
        write(self.root, "ignored.txt")
        write(self.root, "out/a.txt")
        rels = self.rels()
        self.assertIn("tracked.txt", rels)
        self.assertNotIn("ignored.txt", rels)
        self.assertNotIn("out/a.txt", rels)

    def test_binary_by_nul_and_size(self) -> None:
        write(self.root, "text.dat", "texto simples")
        write(self.root, "blob.dat", b"abc\x00def" * 10)  # extensão neutra: só o NUL denuncia
        write(self.root, "big.txt", "x" * (1024 * 1024 + 10))
        rels = self.rels()
        self.assertIn("text.dat", rels)
        self.assertNotIn("blob.dat", rels)
        self.assertNotIn("big.txt", rels)

    def test_size_limit_is_configurable(self) -> None:
        write(self.root, "policy.toml", "[index]\nmax_file_kb = 1\n")
        write(self.root, "small.txt", "x" * 100)
        write(self.root, "medium.txt", "x" * 3000)
        rels = self.rels()
        self.assertIn("small.txt", rels)
        self.assertNotIn("medium.txt", rels)

    def test_build_cache_and_venv_dirs(self) -> None:
        for d in ("build", "target", "node_modules", ".venv", "venv", "__pycache__", ".tox", "pkg/site-packages"):
            write(self.root, f"{d}/f.txt")
        write(self.root, "src/f.txt")
        self.assertEqual(set(self.rels()), {"src/f.txt"})

    def test_extra_excludes_from_policy(self) -> None:
        write(self.root, "policy.toml", '[index]\nexclude_dirs = ["generated"]\nexclude_globs = ["*.min.js"]\n')
        write(self.root, "generated/a.txt")
        write(self.root, "app.min.js")
        write(self.root, "app.js")
        self.assertEqual(set(self.rels()) - {"policy.toml"}, {"app.js"})

    def test_denied_paths_never_indexed(self) -> None:
        write(self.root, "policy.toml", '[paths]\ndeny = [".env", "private/"]\n')
        write(self.root, ".env", "TOKEN=abc")
        write(self.root, "config/.env", "TOKEN=abc")
        write(self.root, "private/notes.txt", "pessoal")
        write(self.root, "ok.txt", "ok")
        rels = set(self.rels())
        self.assertIn("ok.txt", rels)
        self.assertFalse({"private/notes.txt", "config/.env", ".env"} & rels)


class TestReference(ScopeBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, ".gitignore", "dumps/\n")
        write(self.root, "policy.toml", '[index.reference]\nglobs = ["dumps/**/*.txt"]\n')
        write(self.root, "dumps/factory/regs.txt", "register zebra_gain at 0x40")
        write(self.root, "dumps/factory/blob.txt", b"\x00\x01\x02")
        write(self.root, "src/mod.py", "def zebra_gain():\n    return 1\n")

    def test_allowlisted_material_has_reference_kind(self) -> None:
        rels = self.rels()
        self.assertEqual(rels.get("dumps/factory/regs.txt"), "reference")
        self.assertNotIn("dumps/factory/blob.txt", rels)  # binário continua excluído
        self.assertEqual(rels.get("src/mod.py"), "code")

    def test_scope_filter_in_search(self) -> None:
        index_workspace(self.root)
        both = search_many(["zebra_gain"], workspace_root=self.root)["queries"][0]
        self.assertEqual({r["kind"] for r in both["results"]}, {"code", "reference"})
        code = search_many(["zebra_gain"], workspace_root=self.root, scope="code")["queries"][0]
        self.assertEqual({r["kind"] for r in code["results"]}, {"code"})
        ref = search_many(["zebra_gain"], workspace_root=self.root, scope="reference")["queries"][0]
        self.assertEqual({r["path"] for r in ref["results"]}, {"dumps/factory/regs.txt"})
        with self.assertRaises(ValueError):
            search_many(["x"], workspace_root=self.root, scope="nope")

    def test_reference_removed_when_allowlist_dropped(self) -> None:
        index_workspace(self.root)
        write(self.root, "policy.toml", "")
        index_workspace(self.root)
        con = sqlite3.connect(db_path_for(self.root))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM indexed_files WHERE kind='reference'").fetchone()[0], 0)
        finally:
            con.close()


class TestPatchMetadata(ScopeBase):
    def test_summary_and_hunks(self) -> None:
        chunks = extract_patch_chunks(Path("gain.patch"), "gain.patch", PATCH)
        summary = chunks[0]
        self.assertEqual(summary.chunk_type, "patch_summary")
        self.assertIn("drivers/audio.c", summary.content)
        self.assertIn("tools/gain.py", summary.content)
        for symbol in ("audio_set_gain", "apply_gain", "GainTool"):
            self.assertIn(symbol, summary.content)
        self.assertIn("add audio gain control", summary.content)
        self.assertIn("hunks: 2", summary.content)
        hunks = [c for c in chunks if c.chunk_type == "patch_hunk"]
        self.assertEqual(len(hunks), 2)
        self.assertIn("audio_probe", hunks[0].title)

    def test_patch_is_searchable_by_symbol(self) -> None:
        write(self.root, "patches/gain.patch", PATCH)
        write(self.root, "patches/gain2.diff", PATCH.replace("audio_set_gain", "other_fn"))
        index_workspace(self.root)
        res = search_many(["audio_set_gain"], workspace_root=self.root)["queries"][0]
        self.assertTrue(res["results"])
        self.assertEqual(res["results"][0]["path"], "patches/gain.patch")

    def test_non_patch_diff_falls_back_to_generic(self) -> None:
        chunks = extract_patch_chunks(Path("x.diff"), "x.diff", "só texto\nsem diff\n")
        self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
