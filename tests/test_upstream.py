"""Fontes upstream fixadas: baixa uma vez, cache, indexação, validações de segurança. Nenhum teste usa a rede."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ctx import upstream
from ctx.cli import main
from ctx.rag import search_many
from ctx.upstream import UpstreamError, clean_relative_path, list_upstream, resolve_url, upstream_get

POLICY = """
[upstream.demo]
base_url = "https://raw.example.org/demo/{version}/{path}"

[upstream.demo.versions]
v9 = "https://mirror.example.org/demo-v9/{path}"

[upstream.http-only]
base_url = "http://insecure.example.org/{path}"
"""


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeNet:
    """Substitui urlopen: conta chamadas e registra as URLs/cabeçalhos pedidos."""

    def __init__(self, body: bytes = b"def gain_ramp():\n    return 'quokka'\n", error: Exception | None = None) -> None:
        self.body, self.error, self.calls, self.requests = body, error, 0, []

    def __call__(self, req: object, timeout: float = 0) -> FakeResponse:
        self.calls += 1
        self.requests.append(req)
        if self.error:
            raise self.error
        return FakeResponse(self.body)


class UpstreamBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "policy.toml").write_text(POLICY, encoding="utf-8")
        self.net = FakeNet()
        patcher = mock.patch.object(upstream, "_urlopen", self.net)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestCache(UpstreamBase):
    def test_downloads_once_per_version_and_path(self) -> None:
        first = upstream_get(self.root, "demo", "v1", "sound/gain.py")
        self.assertFalse(first["cached"])
        self.assertEqual(self.net.calls, 1)
        expected = self.root / ".ctx" / "upstream" / "demo" / "v1" / "sound" / "gain.py"
        self.assertEqual(Path(first["local_path"]), expected.resolve())
        self.assertEqual(expected.read_bytes(), self.net.body)

        for _ in range(3):
            again = upstream_get(self.root, "demo", "v1", "sound/gain.py")
            self.assertTrue(again["cached"])
            self.assertEqual(again["local_path"], first["local_path"])
        self.assertEqual(self.net.calls, 1)  # nenhuma rede nas chamadas seguintes

    def test_other_version_or_path_downloads_again(self) -> None:
        upstream_get(self.root, "demo", "v1", "a.py")
        upstream_get(self.root, "demo", "v2", "a.py")
        upstream_get(self.root, "demo", "v1", "b.py")
        self.assertEqual(self.net.calls, 3)

    def test_refresh_is_explicit(self) -> None:
        upstream_get(self.root, "demo", "v1", "a.py")
        res = upstream_get(self.root, "demo", "v1", "a.py", refresh=True)
        self.assertFalse(res["cached"])
        self.assertEqual(self.net.calls, 2)

    def test_manifest_records_url_and_hash(self) -> None:
        upstream_get(self.root, "demo", "v1", "dir/a.py")
        manifest = json.loads((self.root / ".ctx" / "upstream" / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["demo/v1/dir/a.py"]
        self.assertEqual(entry["url"], "https://raw.example.org/demo/v1/dir/a.py")
        self.assertEqual(entry["sha256"], hashlib.sha256(self.net.body).hexdigest())

    def test_cache_survives_across_processes_and_reindexes_if_needed(self) -> None:
        upstream_get(self.root, "demo", "v1", "a.py")
        (self.root / ".ctx" / "index.db").unlink()  # índice perdido: cache hit ainda não vai à rede
        res = upstream_get(self.root, "demo", "v1", "a.py")
        self.assertTrue(res["cached"])
        self.assertEqual(self.net.calls, 1)
        hits = search_many(["quokka"], workspace_root=self.root, scope="upstream")["queries"][0]["results"]
        self.assertEqual([h["kind"] for h in hits], ["upstream"])

    def test_failed_download_leaves_no_cache_entry(self) -> None:
        self.net.error = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO())  # type: ignore[arg-type]
        with self.assertRaisesRegex(UpstreamError, "HTTP 404"):
            upstream_get(self.root, "demo", "v1", "a.py")
        self.net.error = None
        self.assertFalse(upstream_get(self.root, "demo", "v1", "a.py")["cached"])  # tenta de novo, sem lixo no cache

    def test_request_carries_no_project_data(self) -> None:
        upstream_get(self.root, "demo", "v1", "a.py")
        req = self.net.requests[0]
        self.assertEqual(req.get_header("User-agent"), "ctx-upstream")
        self.assertEqual(set(h.lower() for h in dict(req.header_items())), {"user-agent"})


class TestIndexing(UpstreamBase):
    def test_indexed_as_upstream_and_survives_reindex(self) -> None:
        from ctx.rag import index_workspace

        upstream_get(self.root, "demo", "v1", "gain.py")
        index_workspace(self.root, force=True)  # .ctx é excluído da varredura, mas o kind upstream é preservado
        res = search_many(["quokka"], workspace_root=self.root)["queries"][0]["results"]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["kind"], "upstream")
        self.assertEqual(search_many(["quokka"], workspace_root=self.root, scope="code")["queries"][0]["count"], 0)

    def test_binary_is_cached_but_not_indexed(self) -> None:
        self.net.body = b"\x00\x01\x02binary"
        res = upstream_get(self.root, "demo", "v1", "blob.bin")
        self.assertTrue(Path(res["local_path"]).exists())
        self.assertEqual(search_many(["binary"], workspace_root=self.root)["queries"][0]["count"], 0)


class TestConfigAndValidation(UpstreamBase):
    def test_url_per_version_overrides_base(self) -> None:
        policy = {"upstream": {"demo": {"base_url": "https://a/{version}/{path}", "versions": {"v9": "https://b/{path}"}}}}
        self.assertEqual(resolve_url(policy, "demo", "v9", "x/y.c"), "https://b/x/y.c")
        self.assertEqual(resolve_url(policy, "demo", "v1", "x/y.c"), "https://a/v1/x/y.c")

    def test_version_mirror_used_from_policy_file(self) -> None:
        res = upstream_get(self.root, "demo", "v9", "a.py")
        self.assertEqual(res["url"], "https://mirror.example.org/demo-v9/a.py")

    def test_unknown_project_lists_declared_ones(self) -> None:
        with self.assertRaisesRegex(UpstreamError, "demo"):
            upstream_get(self.root, "nope", "v1", "a.py")
        self.assertEqual(self.net.calls, 0)

    def test_only_https(self) -> None:
        with self.assertRaisesRegex(UpstreamError, "https"):
            upstream_get(self.root, "http-only", "v1", "a.py")
        self.assertEqual(self.net.calls, 0)

    def test_path_traversal_and_bad_names_rejected(self) -> None:
        for bad in ("../x", "a/../../x", "/etc/passwd", "a\\b", "", ".", "C:/x"):
            with self.assertRaises(UpstreamError, msg=bad):
                upstream_get(self.root, "demo", "v1", bad)
        for project, version in (("../demo", "v1"), ("demo", "../v1"), ("demo", ".."), ("", "v1")):
            with self.assertRaises(UpstreamError, msg=f"{project}/{version}"):
                upstream_get(self.root, project, version, "a.py")
        self.assertEqual(self.net.calls, 0)
        self.assertFalse((self.root / "x").exists())

    def test_symlink_escape_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        version_dir = self.root / ".ctx" / "upstream" / "demo" / "v1"
        version_dir.mkdir(parents=True)
        (version_dir / "link").symlink_to(outside)
        with self.assertRaisesRegex(UpstreamError, "fora do diretório"):
            upstream_get(self.root, "demo", "v1", "link/evil.py")
        self.assertEqual(list(outside.iterdir()), [])

    def test_size_limit(self) -> None:
        with mock.patch.object(upstream, "MAX_BYTES", 10):
            with self.assertRaisesRegex(UpstreamError, "acima de"):
                upstream_get(self.root, "demo", "v1", "big.py")

    def test_clean_relative_path(self) -> None:
        self.assertEqual(clean_relative_path("./a//b/./c.py"), "a/b/c.py")

    def test_list(self) -> None:
        upstream_get(self.root, "demo", "v1", "a.py")
        res = list_upstream(self.root)
        self.assertEqual(sorted(res["projects"]), ["demo", "http-only"])
        self.assertEqual(res["projects"]["demo"]["versions"], ["v9"])
        self.assertEqual([c["key"] for c in res["cached"]], ["demo/v1/a.py"])


class TestCli(UpstreamBase):
    def test_get_prints_local_path_and_second_call_is_cached(self) -> None:
        out1, out2 = io.StringIO(), io.StringIO()
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("sys.stdout", out1), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["upstream", "get", "demo", "v1", "pkg/gain.py"]), 0)
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("sys.stdout", out2), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(["upstream", "get", "demo", "v1", "pkg/gain.py"]), 0)
        self.assertEqual(out1.getvalue(), out2.getvalue())
        self.assertTrue(out1.getvalue().strip().endswith("upstream/demo/v1/pkg/gain.py"))
        self.assertIn("sem rede", err.getvalue())
        self.assertEqual(self.net.calls, 1)

    def test_error_exit_code(self) -> None:
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(main(["upstream", "get", "nope", "v1", "a.py"]), 1)
        self.assertIn("não declarado", out.getvalue())


if __name__ == "__main__":
    unittest.main()
