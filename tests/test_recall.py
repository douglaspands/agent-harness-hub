"""Recall (memória entre sessões), fatos e captura de fim de sessão, sobre um transcript de exemplo (formato Claude Code)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctx import events
from ctx.capture import capture_from_hook, capture_session, extract_candidates, family
from ctx.cli import main
from ctx.facts import (
    FactError, add_candidates, add_fact, list_candidates, remove_fact, review_candidates, search_facts, stale_facts,
)
from ctx.memory_db import memory_path, open_memory
from ctx.config import load_config
from ctx.recall import Privacy, interpret, recall, sync_recall, transcript_files

SESSION = "11111111-aaaa-bbbb-cccc-222222222222"


class Transcript:
    """Monta um .jsonl no formato do Claude Code (message.content com blocos tool_use/tool_result)."""

    def __init__(self, path: Path, session: str = SESSION) -> None:
        self.path, self.session, self.n = path, session, 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def user(self, text: str, ts: str) -> None:
        self._write({"type": "user", "sessionId": self.session, "timestamp": ts, "message": {"role": "user", "content": text}})

    def assistant(self, text: str, ts: str) -> None:
        self._write({"type": "assistant", "sessionId": self.session, "timestamp": ts,
                     "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def call(self, tool: str, tool_input: dict, result: str, ts: str, error: bool = False) -> None:
        self.n += 1
        tid = f"toolu_{self.n}"
        self._write({"type": "assistant", "sessionId": self.session, "timestamp": ts,
                     "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": tool_input}]}})
        self._write({"type": "user", "sessionId": self.session, "timestamp": ts,
                     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}]}})

    def bash(self, command: str, result: str, ts: str, error: bool = False, description: str = "") -> None:
        self.call("Bash", {"command": command, "description": description}, result, ts, error)


class RecallBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projeto"
        self.root.mkdir()
        self.tdir = Path(self._tmp.name) / "transcripts"
        (self.root / "policy.toml").write_text(
            f'[recall]\nsources = [{{ harness = "claude", path = "{self.tdir}/*.jsonl" }}]\n\n[paths]\ndeny = [".env", "~/.ssh"]\n',
            encoding="utf-8",
        )
        self.t = Transcript(self.tdir / f"{SESSION}.jsonl")
        self.build_transcript()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build_transcript(self) -> None:
        t = self.t
        t.user("Preciso compilar o firmware e gravar na placa", "2026-11-01T10:00:00Z")
        t.bash("make firmware", "make: *** No rule to make target 'firmware'.  Stop.\nExit code 2", "2026-11-01T10:01:00Z", error=True)
        t.bash("make all BOARD=rk3399", "[100%] Built target firmware\nok", "2026-11-01T10:05:00Z", description="build do firmware")
        t.bash("make all BOARD=rk3399 CLEAN=1", "[100%] Built target firmware (clean)", "2026-11-03T09:00:00Z", description="build limpo")
        t.call("WebFetch", {"url": "https://mirror.example.org/kernel/patches/0042-audio-gain.patch", "prompt": "patch do audio"},
               "diff --git a/audio.c ...", "2026-11-02T08:00:00Z")
        t.bash("curl -fsSL https://mirror.example.org/tools/flash.sh -o flash.sh", "", "2026-11-02T08:05:00Z")
        t.bash("cat .env", "TOKEN=abc123", "2026-11-02T08:10:00Z")
        t.bash("export API_KEY=sk-live-0123456789abcdefghij && ./deploy.sh", "deploy ok token=ghp_abcdefghijklmnopqrstuvwxyz", "2026-11-02T08:20:00Z")
        t.assistant("Build concluído com BOARD=rk3399.", "2026-11-03T09:01:00Z")


class TestIndexingAndRecall(RecallBase):
    def test_last_successful_build_command(self) -> None:
        res = recall(self.root, "último comando de build que deu certo")
        self.assertTrue(res["intent"]["only_ok"] and res["intent"]["recent"])
        top = res["hits"][0]
        self.assertEqual(top["command"], "make all BOARD=rk3399 CLEAN=1")
        self.assertTrue(top["ok"])
        self.assertEqual(top["tool"], "Bash")
        self.assertIn("Built target", top["result"])
        self.assertTrue(top["when"].startswith("2026-11-03"))
        self.assertTrue(all(h["ok"] for h in res["hits"]))  # o `make firmware` que falhou não aparece

    def test_url_used_for_the_patch(self) -> None:
        res = recall(self.root, "URL usada para o patch audio")
        self.assertTrue(res["intent"]["urls"])
        self.assertIn("https://mirror.example.org/kernel/patches/0042-audio-gain.patch", res["hits"][0]["urls"])

    def test_failed_commands_are_findable_with_error_flag(self) -> None:
        res = recall(self.root, "make que deu erro")
        self.assertTrue(res["intent"]["only_err"])
        self.assertEqual([h["command"] for h in res["hits"]], ["make firmware"])
        self.assertFalse(res["hits"][0]["ok"])

    def test_messages_are_indexed_too(self) -> None:
        res = recall(self.root, "firmware placa", kind="user")
        self.assertEqual(res["hits"][0]["kind"], "user")
        self.assertIn("gravar na placa", res["hits"][0]["text"])

    def test_filters_tool_since_and_top(self) -> None:
        self.assertEqual({h["tool"] for h in recall(self.root, "https", tool="WebFetch", raw=True)["hits"]}, {"WebFetch"})
        self.assertEqual(recall(self.root, "make", top=1)["hits"].__len__(), 1)
        self.assertEqual(recall(self.root, "make", since="1h")["hits"], [])  # transcript é de 2026-11; fora da janela
        with self.assertRaises(ValueError):
            recall(self.root, "make", since="ontem")

    def test_result_is_truncated_and_success_flag_comes_from_the_result(self) -> None:
        self.t.bash("cat big.log", "x" * 5000 + "\nFIM", "2026-11-04T10:00:00Z")
        hit = recall(self.root, "big.log", raw=True)["hits"][0]
        self.assertLessEqual(len(hit["result"]), 620)
        self.assertTrue(hit["result"].endswith("FIM"))
        self.assertIs(hit["ok"], True)

    def test_incremental_sync_reads_only_the_new_tail(self) -> None:
        first = sync_recall(self.root)
        self.assertGreater(first["added"], 5)
        self.assertEqual(sync_recall(self.root)["added"], 0)
        self.t.bash("make install", "installed", "2026-11-05T10:00:00Z")
        again = sync_recall(self.root)
        self.assertEqual(again["added"], 1)
        self.assertEqual(recall(self.root, "make install", raw=True, sync=False)["hits"][0]["result"], "installed")

    def test_result_arriving_in_a_later_chunk_updates_the_call(self) -> None:
        tid = "toolu_split"
        self.t._write({"type": "assistant", "sessionId": SESSION, "timestamp": "2026-11-06T10:00:00Z",
                       "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "make slow"}}]}})
        sync_recall(self.root)
        self.assertIsNone(recall(self.root, "make slow", raw=True, sync=False)["hits"][0]["ok"])  # ainda sem resultado
        self.t._write({"type": "user", "sessionId": SESSION, "timestamp": "2026-11-06T10:01:00Z",
                       "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "done", "is_error": False}]}})
        sync_recall(self.root)
        hit = recall(self.root, "make slow", raw=True, sync=False)["hits"][0]
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["result"], "done")

    def test_partial_last_line_waits_for_completion(self) -> None:
        with open(self.t.path, "a", encoding="utf-8") as f:
            f.write('{"type": "user", "sessionId": "x", "timestamp": "2026-11-07T10:00:00Z", "message": {"role": "user", "content": "meia')
        sync_recall(self.root)
        self.assertEqual(recall(self.root, "meia", raw=True, sync=False)["hits"], [])
        with open(self.t.path, "a", encoding="utf-8") as f:
            f.write(' linha"}}\n')
        sync_recall(self.root)
        self.assertEqual(len(recall(self.root, "meia", raw=True, sync=False)["hits"]), 1)

    def test_malformed_lines_are_skipped(self) -> None:
        with open(self.t.path, "a", encoding="utf-8") as f:
            f.write("não é json\n[1,2,3]\n\n")
        self.t.bash("make ok-after-garbage", "fine", "2026-11-08T10:00:00Z")
        self.assertEqual(len(recall(self.root, "ok-after-garbage", raw=True)["hits"]), 1)

    def test_rewritten_transcript_is_reindexed_without_duplicates(self) -> None:
        sync_recall(self.root)
        self.t.path.write_text("", encoding="utf-8")
        t2 = Transcript(self.t.path)
        t2.bash("make novo", "novo ok", "2026-11-09T10:00:00Z")
        sync_recall(self.root)
        self.assertEqual(recall(self.root, "make", raw=True, sync=False)["hits"][0]["command"], "make novo")
        self.assertEqual(len(recall(self.root, "make", raw=True, sync=False, top=50)["hits"]), 1)

    def test_unsupported_harness_is_reported(self) -> None:
        (self.root / "policy.toml").write_text(
            f'[recall]\nsources = [{{ harness = "outro", path = "{self.tdir}/*.jsonl" }}]\n', encoding="utf-8"
        )
        res = sync_recall(self.root)
        self.assertEqual(res["unsupported_harnesses"], ["outro"])
        self.assertEqual(res["added"], 0)

    def test_default_source_uses_project_slug(self) -> None:
        (self.root / "policy.toml").write_text("", encoding="utf-8")
        files = transcript_files(self.root, load_config(self.root))
        self.assertEqual(files, [])  # ~/.claude/projects/<slug>/ não existe para este projeto temporário

    def test_interpret(self) -> None:
        i = interpret("último comando de build que deu certo")
        self.assertEqual((i.terms, i.only_ok, i.recent, i.commands), (["build"], True, True, True))
        self.assertEqual(interpret("URL usada para o patch X").terms, ["patch", "x"])


class TestPrivacy(RecallBase):
    def test_denied_paths_are_not_indexed(self) -> None:
        sync_recall(self.root)
        self.assertEqual(recall(self.root, "cat .env", raw=True, sync=False)["hits"], [])
        self.assertEqual(recall(self.root, "abc123", raw=True, sync=False)["hits"], [])
        self.assertEqual(recall(self.root, "TOKEN", raw=True, sync=False)["hits"], [])

    def test_secrets_are_redacted(self) -> None:
        res = recall(self.root, "deploy", raw=True)
        blob = json.dumps(res["hits"])
        self.assertNotIn("sk-live-0123456789abcdefghij", blob)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", blob)
        self.assertIn("[REDACTED]", blob)

    def test_policy_change_filters_already_indexed_rows(self) -> None:
        sync_recall(self.root)
        self.assertTrue(recall(self.root, "flash.sh", raw=True, sync=False)["hits"])
        policy = (self.root / "policy.toml").read_text(encoding="utf-8")
        (self.root / "policy.toml").write_text(policy.replace('deny = [".env", "~/.ssh"]', 'deny = [".env", "~/.ssh", "flash.sh"]'), encoding="utf-8")
        self.assertEqual(recall(self.root, "flash.sh", raw=True, sync=False)["hits"], [])

    def test_privacy_unit(self) -> None:
        p = Privacy({"paths": {"deny": [".env", "~/.ssh", "secrets/"]}}, [r"\bpw=\S+"], Path("/proj"))
        self.assertTrue(p.blocked("cat .env"))
        self.assertTrue(p.blocked("ls ~/.ssh/id_rsa"))
        self.assertTrue(p.blocked("cat /proj/secrets/x"))
        self.assertFalse(p.blocked("cat .env.example"))
        self.assertFalse(p.blocked("cat my.environment"))
        self.assertEqual(p.clean("a pw=hunter2 b", 100), "a [REDACTED] b")

    def test_nothing_leaves_the_machine(self) -> None:
        with mock.patch("socket.socket", side_effect=AssertionError("rede proibida")):
            recall(self.root, "make")
            add_fact(self.root, "x", "policy.toml:1")


class TestFacts(RecallBase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "notes.txt").write_text("linha um\nGAIN_MAX = 12\nlinha três\n", encoding="utf-8")

    def test_add_search_roundtrip(self) -> None:
        res = add_fact(self.root, "O ganho máximo medido é 12 dB", "notes.txt:2", "audio, Medicao")
        self.assertFalse(res["duplicate"])
        hits = search_facts(self.root, "ganho máximo")
        self.assertEqual([h["id"] for h in hits], [res["id"]])
        self.assertEqual(hits[0]["tags"], ["audio", "medicao"])
        self.assertEqual(hits[0]["status"], "ok")
        self.assertEqual(search_facts(self.root, "", tag="audio")[0]["id"], res["id"])
        self.assertEqual(search_facts(self.root, "", tag="outra"), [])

    def test_duplicate_merges_tags(self) -> None:
        a = add_fact(self.root, "fato", "notes.txt:1", "a")
        b = add_fact(self.root, "fato", "notes.txt:1", "b")
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(search_facts(self.root, "fato")[0]["tags"], ["a", "b"])

    def test_source_is_required_and_must_exist(self) -> None:
        for bad in ("", "   ", "nao-existe.txt", "notes.txt:99", "notes.txt:0"):
            with self.assertRaises(FactError, msg=bad):
                add_fact(self.root, "fato", bad)
        self.assertEqual(add_fact(self.root, "web", "https://exemplo.org/doc")["source_kind"], "url")

    def test_denied_source_and_text_are_refused(self) -> None:
        (self.root / ".env").write_text("A=1\n", encoding="utf-8")
        with self.assertRaisesRegex(FactError, "negado"):
            add_fact(self.root, "fato", ".env:1")
        with self.assertRaisesRegex(FactError, "negado"):
            add_fact(self.root, "veja o .env", "notes.txt:1")

    def test_stale_when_cited_line_changes_but_not_for_other_lines(self) -> None:
        fid = add_fact(self.root, "GAIN_MAX é 12", "notes.txt:2")["id"]
        (self.root / "notes.txt").write_text("linha UM mudou\nGAIN_MAX = 12\nlinha três\n", encoding="utf-8")
        self.assertEqual(stale_facts(self.root)["stale"], [])  # outra linha mudou: o fato segue válido
        (self.root / "notes.txt").write_text("linha um\nGAIN_MAX = 15\nlinha três\n", encoding="utf-8")
        stale = stale_facts(self.root)["stale"]
        self.assertEqual([f["id"] for f in stale], [fid])
        self.assertEqual(stale[0]["status"], "stale")
        self.assertEqual(search_facts(self.root, "GAIN_MAX")[0]["status"], "stale")

    def test_stale_file_level_missing_and_url(self) -> None:
        whole = add_fact(self.root, "arquivo inteiro", "notes.txt")["id"]
        url = add_fact(self.root, "doc web", "https://exemplo.org/doc")["id"]
        (self.root / "notes.txt").write_text("outro conteúdo\n", encoding="utf-8")
        res = stale_facts(self.root)
        self.assertEqual([f["id"] for f in res["stale"]], [whole])
        self.assertEqual([f["id"] for f in res["unverifiable"]], [url])  # URL: só listada, sem rede
        (self.root / "notes.txt").unlink()
        self.assertEqual(stale_facts(self.root)["stale"][0]["status"], "missing")

    def test_remove(self) -> None:
        fid = add_fact(self.root, "descartável", "notes.txt:1")["id"]
        self.assertTrue(remove_fact(self.root, fid))
        self.assertEqual(search_facts(self.root, "descartável"), [])
        self.assertFalse(remove_fact(self.root, fid))

    def test_facts_survive_memory_index_rebuild_and_corruption_keeps_backup(self) -> None:
        add_fact(self.root, "duradouro", "notes.txt:1")
        sync_recall(self.root, rebuild=True)
        self.assertEqual(len(search_facts(self.root, "duradouro")), 1)
        path = memory_path(self.root)
        path.write_bytes(b"corrompido" * 500)
        open_memory(self.root).close()
        self.assertTrue(list(path.parent.glob("memory.db.corrupt-*")), "o banco ruim é preservado, não apagado")


class TestCapture(RecallBase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "policy.toml").write_text(
            (self.root / "policy.toml").read_text(encoding="utf-8").replace("[recall]\n", "[recall]\ncapture_on_stop = true\n", 1),
            encoding="utf-8",
        )

    def test_candidates_from_resolved_errors_commands_and_urls(self) -> None:
        res = capture_session(self.root, self.t.path)
        self.assertEqual(res["status"], "ok")
        texts = [c["text"] for c in list_candidates(self.root)]
        joined = "\n".join(texts)
        self.assertTrue(any("Erro em `make firmware`" in t and "Resolvido com `make all BOARD=rk3399`" in t for t in texts), joined)
        self.assertIn("Comando que funcionou: `make all BOARD=rk3399 CLEAN=1`", joined)
        self.assertIn("URL usada com sucesso: https://mirror.example.org/kernel/patches/0042-audio-gain.patch", joined)
        self.assertIn("https://mirror.example.org/tools/flash.sh", joined)
        self.assertNotIn(".env", joined)  # caminho negado: nada de `cat .env`
        self.assertNotIn("sk-live", joined)

    def test_candidates_never_become_facts_without_review(self) -> None:
        capture_session(self.root, self.t.path)
        self.assertEqual(search_facts(self.root, "", top=50), [])
        cands = list_candidates(self.root)
        first = next(c for c in cands if c["text"].startswith("Comando que funcionou"))
        report = review_candidates(self.root, accept=[first["id"]], reject=[c["id"] for c in cands if c["id"] != first["id"]])
        self.assertIn("aceito como fato", [r for r in report if r["id"] == first["id"]][0]["result"])
        facts = search_facts(self.root, "", top=50)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["status"], "ok")  # fonte = linha do transcript, verificável
        self.assertEqual(list_candidates(self.root), [])

    def test_second_capture_does_not_duplicate(self) -> None:
        a = capture_session(self.root, self.t.path)
        b = capture_session(self.root, self.t.path)
        self.assertGreater(a["added"], 0)
        self.assertEqual(b["added"], 0)

    def test_rejected_candidate_is_not_proposed_again(self) -> None:
        capture_session(self.root, self.t.path)
        ids = [c["id"] for c in list_candidates(self.root)]
        review_candidates(self.root, accept=[], reject=ids)
        self.assertEqual(capture_session(self.root, self.t.path)["added"], 0)

    def test_disabled_by_default_and_force(self) -> None:
        (self.root / "policy.toml").write_text(
            (self.root / "policy.toml").read_text(encoding="utf-8").replace("capture_on_stop = true", "capture_on_stop = false"), encoding="utf-8"
        )
        self.assertEqual(capture_session(self.root, self.t.path)["status"], "disabled")
        self.assertEqual(capture_session(self.root, self.t.path, force=True)["status"], "ok")

    def test_hook_entry_never_raises(self) -> None:
        payload = json.dumps({"session_id": SESSION, "transcript_path": str(self.t.path), "hook_event_name": "Stop"})
        self.assertEqual(capture_from_hook(self.root, payload)["status"], "ok")
        for bad in ("", "{{{", "[]", json.dumps({"transcript_path": "/nao/existe.jsonl"})):
            self.assertIn(capture_from_hook(self.root, bad)["status"], ("no_transcript", "ok", "error"))

    def test_hook_without_path_uses_latest_transcript(self) -> None:
        self.assertEqual(capture_from_hook(self.root, "{}")["transcript"], str(self.t.path))

    def test_family_and_cap(self) -> None:
        self.assertEqual(family("make all BOARD=x"), ("make", "all"))
        self.assertEqual(family("/usr/bin/cargo build --release"), ("cargo", "build"))
        self.assertEqual(family("uv --version"), ("uv", ""))
        cfg = load_config(self.root)
        cfg["recall"]["max_candidates"] = 2
        calls = [
            {"session": "s", "tool": "Bash", "command": f"make t{i}", "text": "", "result": "", "ok": 1, "src": f"f:{i}"} for i in range(6)
        ]
        self.assertEqual(len(extract_candidates(calls, cfg, Privacy({}, [], self.root), ".venv/bin/ctx")), 2)

    def test_candidates_dedupe_helper(self) -> None:
        c = {"text": "Comando que funcionou: `make x`", "source": "f:1", "tags": ["a"]}
        self.assertEqual(add_candidates(self.root, [c, dict(c)]), 1)


class TestCli(RecallBase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("sys.stdout", out), mock.patch("ctx.cli.events.log_event") as log:
            code = main(list(argv))
        self.last_event = log.call_args.args[1] if log.call_args else None
        return code, out.getvalue()

    def test_recall_cli_and_event(self) -> None:
        code, out = self.run_cli("recall", "último", "comando", "de", "build", "que", "deu", "certo")
        self.assertEqual(code, 0)
        self.assertIn("make all BOARD=rk3399 CLEAN=1", out)
        self.assertEqual((self.last_event["cmd"], self.last_event["hit"]), ("recall", True))
        code, out = self.run_cli("recall", "inexistentezzz")
        self.assertIn("Nada encontrado", out)
        self.assertFalse(self.last_event["hit"])

    def test_fact_cli(self) -> None:
        (self.root / "notes.txt").write_text("a\nb\n", encoding="utf-8")
        code, out = self.run_cli("fact", "add", "valor medido", "--source", "notes.txt:2", "--tags", "x,y")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("fact", "search", "valor")
        self.assertIn("valor medido", out)
        self.assertEqual(self.run_cli("fact", "stale")[0], 0)
        (self.root / "notes.txt").write_text("a\nmudou\n", encoding="utf-8")
        code, out = self.run_cli("fact", "stale")
        self.assertEqual(code, 1)
        self.assertIn("valor medido", out)
        self.assertEqual(self.run_cli("fact", "add", "x", "--source", "nao-existe")[0], 1)

    def test_recall_capture_cli(self) -> None:
        code, out = self.run_cli("recall", "capture", "--file", str(self.t.path))
        self.assertEqual(code, 0)
        self.assertIn("candidato", out)
        code, out = self.run_cli("fact", "review")
        self.assertIn("pendente", out)


if __name__ == "__main__":
    unittest.main()
