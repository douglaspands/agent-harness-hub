"""Medição por sessão (`ctx metrics`) e avaliação de eficiência (`ctx evaluate`) sobre transcripts e eventos sintéticos."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ctx.cli import main
from ctx.evaluate import COMPENSA, MARGINAL, NAO, SEM_DADOS, evaluate, instruction_overhead, parse_window
from ctx.metrics import analyze, analyze_session, count_queries, ctx_subcommand, is_detour

BASE = 1_793_000_000  # época fixa (nov/2026)
DAY = 86_400


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def bash(cmd: str, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}


def result(tid: str, text: str = "ok") -> dict:
    return {"type": "tool_result", "tool_use_id": tid, "content": text}


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / ".ctx").mkdir(parents=True)
        self.tdir = Path(self._tmp.name) / "transcripts"
        self.tdir.mkdir()
        self.events: list[dict] = []
        self.n = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def policy(self, text: str) -> None:
        (self.root / "policy.toml").write_text(text, encoding="utf-8")

    def session(self, day: int, model: str, cmds: list[str], results: dict[int, str] | None = None) -> float:
        """Cria uma sessão no dia `day` com os comandos Bash dados; retorna o início (epoch)."""
        self.n += 1
        start = BASE + day * DAY
        lines = []
        for i, cmd in enumerate(cmds):
            tid = f"t{self.n}_{i}"
            lines.append({"timestamp": iso(start + i * 10), "message": {"model": model, "content": [bash(cmd, tid)]}})
            lines.append({"timestamp": iso(start + i * 10 + 1), "message": {"content": [result(tid, (results or {}).get(i, "ok"))]}})
        (self.tdir / f"s{self.n:03d}.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return start

    def event(self, ts: float, **fields: object) -> None:
        self.events.append({"ts": ts, **fields})

    def flush_events(self) -> None:
        (self.root / ".ctx" / "events.jsonl").write_text("\n".join(json.dumps(e) for e in self.events) + "\n", encoding="utf-8")

    def run_eval(self, **kw: object) -> dict:
        self.flush_events()
        return evaluate(self.root, transcripts_dir=self.tdir, **kw)  # type: ignore[arg-type]

    def feature(self, res: dict, name: str) -> dict:
        return next(f for f in res["features"] if f["feature"] == name)


class TestParsingHelpers(unittest.TestCase):
    def test_ctx_subcommand_and_queries(self) -> None:
        self.assertEqual(ctx_subcommand('.venv/bin/ctx search "a b" c --lines 25 --path src'), ("search", ["a b", "c", "--lines", "25", "--path", "src"]))
        self.assertEqual(ctx_subcommand("uv run ctx status")[0], "status")
        self.assertEqual(ctx_subcommand("ctx --version"), None)
        self.assertIsNone(ctx_subcommand("rg ctx"))
        self.assertIsNone(ctx_subcommand("git status"))
        self.assertEqual(count_queries(["a", "b", "c", "--lines", "25", "--path", "src", "--top", "3"]), 3)
        self.assertEqual(count_queries(["q", "|", "head"]), 1)

    def test_detours(self) -> None:
        for cmd in ("rg -n foo src", "grep -r foo .", "sed -n '1,20p' a.py"):
            self.assertTrue(is_detour(cmd), cmd)
        for cmd in ("sed -i s/a/b/ f", "git status", ".venv/bin/ctx search x", ""):
            self.assertFalse(is_detour(cmd), cmd)

    def test_window(self) -> None:
        self.assertEqual(parse_window(None), 0.0)
        self.assertGreater(parse_window("7d"), 0)
        with self.assertRaises(ValueError):
            parse_window("ontem")


class TestMetricsPerSession(Workspace):
    def test_counters(self) -> None:
        url = "https://mirror.example.org/a.patch"
        self.session(0, "claude-haiku-5-5", [
            '.venv/bin/ctx search "um" "dois" "tres" --lines 25',
            ".venv/bin/ctx map src", ".venv/bin/ctx recall build", ".venv/bin/ctx fact search x",
            ".venv/bin/ctx upstream get p v1 a", ".venv/bin/ctx recipe run copy-verify a b", ".venv/bin/ctx recipe list",
            "rg -n foo src", "grep -rn bar .", "sed -n '1,5p' f.py",
            f"curl -fsSL {url} -o a.patch", f"curl -fsSL {url} -o b.patch",
            "make build", "make build", "git status", "git status",
            "cat x.py",
        ], results={15: "no", 3: "PreToolUse:Bash hook error: Use a Receita One-Shot"})
        s = analyze_session(next(self.tdir.glob("*.jsonl")))
        self.assertEqual((s["ctx_search"], s["ctx_queries"]), (1, 3))
        self.assertEqual((s["ctx_map"], s["ctx_recall"], s["ctx_fact"], s["ctx_upstream"], s["ctx_recipe"]), (1, 1, 1, 1, 1))
        self.assertEqual(s["rg_detours"], 3)
        self.assertEqual(s["redownloads"], 1)
        self.assertEqual(s["repeated_cmds"], 1)  # `make build` (git status é trivial)
        self.assertEqual(s["denials"], 1)
        self.assertEqual(s["bash_read"], 2)  # `sed -n` e `cat x.py` (mesmo classificador do coach)
        self.assertEqual(s["calls"], 17)
        self.assertGreater(s["result_chars"], 0)
        self.assertEqual(s["model"], "claude-haiku-5-5")

    def test_events_are_assigned_to_sessions_and_summarised(self) -> None:
        a = self.session(0, "m", [".venv/bin/ctx search x"])
        b = self.session(3, "m", [".venv/bin/ctx recipe run copy-verify a b"])
        self.event(a + 5, cmd="search", latency_ms=12.0, exit=0)
        self.event(b + 5, cmd="search", latency_ms=40.0, exit=0)
        self.event(b + 6, cmd="search", latency_ms=300.0, exit=0)
        self.event(b + 7, cmd="recipe run", latency_ms=90.0, recipe="copy-verify", steps_saved=4, status="ok")
        self.event(b + 8, cmd="recall", latency_ms=20.0, hit=True)
        self.event(b + 9, cmd="recall", latency_ms=20.0, hit=False)
        self.event(b + 10, cmd="upstream get", latency_ms=5.0, cached=True)
        self.event(b + 11, cmd="hint", recipe="copy-verify")
        self.event(BASE + 90 * DAY, cmd="search", latency_ms=1.0)  # fora de qualquer sessão
        self.flush_events()
        res = analyze(self.tdir, self.root)
        sa, sb = res["sessions"]
        self.assertEqual(len(sa["events"]), 1)
        self.assertEqual(sb["ctx_events"]["recipe_steps_saved"], 4)
        self.assertEqual((sb["ctx_events"]["recall_queries"], sb["ctx_events"]["recall_hits"]), (2, 1))
        self.assertEqual((sb["ctx_events"]["upstream_gets"], sb["ctx_events"]["upstream_cached"], sb["ctx_events"]["hints"]), (1, 1, 1))
        self.assertEqual(sb["ctx_events"]["latency"]["search"]["max_ms"], 300.0)
        self.assertEqual(res["orphan_events"], 1)
        self.assertEqual(res["totals"]["ctx_search_pct"], 100.0)

    def test_since_filters_sessions(self) -> None:
        self.session(0, "m", [".venv/bin/ctx status"])
        self.assertEqual(len(analyze(self.tdir, self.root, since_ts=BASE + DAY)["sessions"]), 0)
        self.assertEqual(len(analyze(self.tdir, self.root, since_ts=BASE - DAY)["sessions"]), 1)


class TestEvaluateVerdicts(Workspace):
    def healthy(self, sessions: int = 10) -> None:
        self.policy("[metrics.evaluate]\nmin_net_saving_tokens = 500\n")
        for d in range(sessions):
            start = self.session(d, "claude-sonnet-5-5", ['.venv/bin/ctx search "a" "b" "c"', "rg -n x src", ".venv/bin/ctx recipe run copy-verify a b"])
            if d % 2 == 0:
                self.event(start + 21, cmd="recipe run", recipe="copy-verify", steps_saved=4, status="ok", latency_ms=50.0)
            self.event(start + 22, cmd="recall", hit=d < 8, latency_ms=10.0)
            self.event(start + 23, cmd="search", latency_ms=8.0)

    def test_too_few_sessions_is_no_data(self) -> None:
        self.healthy(sessions=2)
        res = self.run_eval()
        self.assertEqual(res["overall"]["verdict"], SEM_DADOS)
        self.assertEqual(self.feature(res, "busca e índice")["verdict"], SEM_DADOS)
        self.assertTrue(any("Amostra insuficiente" in r for r in res["recommendations"]))

    def test_healthy_usage_pays_off_with_net_balance(self) -> None:
        self.healthy()
        res = self.run_eval()
        o = res["overall"]
        self.assertEqual(self.feature(res, "busca e índice")["verdict"], COMPENSA)  # 10 ctx vs 10 rg => 50%
        self.assertEqual(self.feature(res, "receitas")["verdict"], COMPENSA)  # 5 execuções x 4 = 20 chamadas
        self.assertEqual(self.feature(res, "recall (transcripts)")["verdict"], COMPENSA)  # 8/10
        # busca: 10x(3-1)=20; receitas: 20; recall: 8 acertos x 2 = 16  => 56 chamadas x 250 tokens
        self.assertEqual(o["saved_calls"], 56)
        self.assertEqual(o["saved_tokens"], 14_000)
        self.assertEqual(o["overhead_tokens_per_session"], 0)  # sem bloco de instruções no projeto de teste
        self.assertEqual(o["net_tokens"], 14_000)
        self.assertEqual(o["verdict"], COMPENSA)
        self.assertIn("estimativa", json.dumps(res["assumptions"], ensure_ascii=False))

    def test_instruction_overhead_can_outweigh_savings(self) -> None:
        self.healthy()
        block = "<!-- AUTOGENERATED_BY_CTX_START -->\n" + "x" * 40_000 + "\n<!-- AUTOGENERATED_BY_CTX_END -->"
        (self.root / "CLAUDE.md").write_text("# meu projeto\n" + block, encoding="utf-8")
        res = self.run_eval()
        self.assertGreaterEqual(res["overall"]["overhead_tokens_per_session"], 10_000)
        self.assertLess(res["overall"]["net_tokens"], 0)
        self.assertEqual(res["overall"]["verdict"], NAO)
        self.assertTrue(any("custo fixo" in r for r in res["recommendations"]))

    def test_marginal_when_gain_is_below_threshold(self) -> None:
        self.healthy()
        self.policy("[metrics.evaluate]\nmin_net_saving_tokens = 5000\n")
        self.assertEqual(self.run_eval()["overall"]["verdict"], MARGINAL)  # 1400/sessão: positivo mas abaixo do limiar

    def test_unused_features_are_flagged_only_when_ctx_is_adopted(self) -> None:
        self.policy("")
        for d in range(6):
            self.session(d, "m", [".venv/bin/ctx search x", "rg foo", "rg bar"])
        res = self.run_eval()
        self.assertEqual(self.feature(res, "recall (transcripts)")["verdict"], NAO)  # ctx adotado (6 sessões) e recall nunca usado
        self.assertEqual(self.feature(res, "busca e índice")["verdict"], MARGINAL)  # adoção 33%
        self.assertEqual(self.feature(res, "fatos verificados")["verdict"], SEM_DADOS)
        self.assertEqual(self.feature(res, "receitas")["verdict"], SEM_DADOS)
        self.assertTrue(any("ctx recall" in r for r in res["recommendations"]))

    def test_low_search_adoption_says_not_worth_it(self) -> None:
        self.policy("")
        for d in range(6):
            self.session(d, "m", [".venv/bin/ctx status"] + ["rg foo"] * 5 + [".venv/bin/ctx search x"])
        res = self.run_eval()
        self.assertEqual(self.feature(res, "busca e índice")["verdict"], NAO)  # 6 vs 30 => 17%
        self.assertTrue(any("prefere rg/grep/sed" in r for r in res["recommendations"]))

    def test_redownloads_without_upstream_recommend_declaring_it(self) -> None:
        self.policy("")
        for d in range(6):
            self.session(d, "m", ["curl -fsSL https://x.example.org/a -o a", "curl -fsSL https://x.example.org/a -o b", ".venv/bin/ctx status"])
        res = self.run_eval()
        self.assertEqual(self.feature(res, "upstream em cache")["verdict"], NAO)
        self.assertTrue(any("[upstream." in r for r in res["recommendations"]))

    def test_upstream_cache_hits_count_as_savings(self) -> None:
        self.policy("")
        for d in range(6):
            start = self.session(d, "m", [".venv/bin/ctx upstream get p v1 a"])
            self.event(start + 5, cmd="upstream get", cached=d > 0, latency_ms=3.0)
        f = self.feature(self.run_eval(), "upstream em cache")
        self.assertEqual((f["verdict"], f["saved_calls"]), (COMPENSA, 5))

    def test_coaching_trend(self) -> None:
        self.policy("")
        deny = "PreToolUse:Bash hook error: Encadeamento de comandos"
        for d in range(8):
            n_deny = 4 if d < 4 else 0
            self.session(d, "m", [".venv/bin/ctx status"] + ["cmd"] * n_deny, results={i: deny for i in range(1, 1 + n_deny)})
        self.assertEqual(self.feature(self.run_eval(), "coaching do guardrail")["verdict"], COMPENSA)  # 4.0 -> 0.0
        self.tearDown()
        self.setUp()
        self.policy("")
        for d in range(8):
            n_deny = 0 if d < 4 else 3
            self.session(d, "m", [".venv/bin/ctx status"] + ["cmd"] * n_deny, results={i: deny for i in range(1, 1 + n_deny)})
        self.assertEqual(self.feature(self.run_eval(), "coaching do guardrail")["verdict"], NAO)

    def test_denials_by_rule_and_repeats(self) -> None:
        """M-19: por id de regra (guardrail 0.5) e, em transcripts antigos, pelo início da mensagem."""
        self.policy("")
        chain = "PreToolUse:Bash hook error: Encadeamento ... proibido. [regra: chaining]"
        old = "PreToolUse:Bash hook error: Comando bloqueado (padrão proibido: 'mkfs')."
        for d in range(6):
            self.session(d, "m", [".venv/bin/ctx status", "a", "b", "c"], results={1: chain, 2: chain, 3: old})
        o = self.run_eval()["overall"]
        self.assertEqual(o["denial_rules"][0], {"rule": "chaining", "count": 12, "tokens": 3000})
        self.assertEqual(o["denial_rules"][1]["count"], 6)
        self.assertIn("padrão proibido", o["denial_rules"][1]["rule"])
        self.assertEqual(o["denial_repeats"], 6)  # o 2º chaining de cada sessão

    def test_provisional_verdict_and_fact_hint(self) -> None:
        """M-20 e M-21: abaixo de confident_sessions o veredito é provisório; sem fatos, recomenda `ctx fact add`."""
        self.healthy()
        res = self.run_eval()
        self.assertEqual(res["overall"]["confidence"], "provisório")
        self.assertTrue(any("provisório" in r for r in res["recommendations"]))
        self.assertTrue(any("ctx fact add" in r for r in res["recommendations"]))
        self.policy("[metrics.evaluate]\nconfident_sessions = 10\n")
        self.assertEqual(self.run_eval()["overall"]["confidence"], "ok")

    def test_search_adoption_per_session(self) -> None:
        """M-22: a evidência traz a adoção por sessão, além de por chamada."""
        self.policy("")
        for d in range(6):
            self.session(d, "m", [".venv/bin/ctx status", "rg foo"] + ([".venv/bin/ctx search x"] if d < 2 else []))
        evidence = self.feature(self.run_eval(), "busca e índice")["evidence"]
        self.assertIn("2/6 sessões usaram `ctx search`", evidence)
        self.assertIn("6/6 algum comando do ctx", evidence)

    def test_facts_never_consulted_and_stale(self) -> None:
        self.policy("")
        (self.root / "notes.txt").write_text("a\nb\n", encoding="utf-8")
        from ctx.facts import add_fact

        add_fact(self.root, "valor", "notes.txt:2")
        for d in range(6):
            self.session(d, "m", [".venv/bin/ctx status"])
        self.assertEqual(self.feature(self.run_eval(), "fatos verificados")["verdict"], NAO)  # 1 fato, 0 consultas


class TestByModel(Workspace):
    def test_comparison_per_model_and_small_samples(self) -> None:
        self.policy("")
        for d in range(4):  # haiku com ctx: poucas chamadas
            self.session(d, "claude-haiku-5-5", [".venv/bin/ctx search x"] + ["a"] * 4)
        for d in range(4, 8):  # haiku sem ctx: muitas chamadas
            self.session(d, "claude-haiku-5-5", ["rg x"] + ["b"] * 14)
        for d in range(8, 10):  # sonnet: amostra pequena
            self.session(d, "claude-sonnet-5-5", [".venv/bin/ctx status"] + ["c"] * 3)
        res = self.run_eval()
        models = {m["model"]: m for m in res["models"]}
        haiku = models["claude-haiku-5-5"]
        self.assertEqual((haiku["with_ctx"], haiku["without_ctx"], haiku["comparison"]), (4, 4, "ok"))
        self.assertEqual(haiku["avg_calls_with"], 5.0)
        self.assertEqual(haiku["avg_calls_without"], 15.0)
        self.assertEqual(haiku["calls_delta_pct"], -66.7)
        self.assertTrue(models["claude-sonnet-5-5"]["comparison"].startswith("amostra pequena"))

    def test_model_filter_restricts_everything(self) -> None:
        self.policy("")
        for d in range(6):
            self.session(d, "claude-haiku-5-5", [".venv/bin/ctx search a b"])
        for d in range(6, 12):
            self.session(d, "claude-sonnet-5-5", ["rg x"] * 3 + [".venv/bin/ctx status"])
        res = self.run_eval(model="haiku")
        self.assertEqual(res["overall"]["sessions"], 6)
        self.assertEqual([m["model"] for m in res["models"]], ["claude-haiku-5-5"])
        self.assertIn("rg/grep/sed", self.feature(res, "busca e índice")["evidence"])
        self.assertIn("0 desvios", self.feature(res, "busca e índice")["evidence"])


class TestOverheadAndCli(Workspace):
    def test_overhead_reads_generated_block_and_skill(self) -> None:
        (self.root / "AGENTS.md").write_text("<!-- AUTOGENERATED_BY_CTX_START -->" + "a" * 400 + "<!-- AUTOGENERATED_BY_CTX_END -->", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("antes\n<!-- AUTOGENERATED_BY_CTX_START -->" + "b" * 800 + "<!-- AUTOGENERATED_BY_CTX_END -->\ndepois", encoding="utf-8")
        skill = self.root / ".claude" / "skills" / "workspace-ctx" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: workspace-ctx\ndescription: " + "d" * 120 + "\n---\n", encoding="utf-8")
        o = instruction_overhead(self.root)
        self.assertEqual(set(o["per_file"]), {"AGENTS.md", "CLAUDE.md"})
        self.assertGreater(o["block_tokens"], 200)  # o maior dos arquivos
        self.assertEqual(o["skill_description_tokens"], 30)
        self.assertEqual(o["per_session"], o["block_tokens"] + 30)

    def test_no_transcripts_dir_is_no_data_not_a_crash(self) -> None:
        res = evaluate(self.root, transcripts_dir=self.root / "nao-existe")
        self.assertEqual(res["overall"]["verdict"], SEM_DADOS)
        self.assertEqual(res["overall"]["sessions"], 0)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("ctx.evaluate.transcripts_dir_for", return_value=self.tdir), \
                mock.patch("ctx.cli.transcripts_dir_for", return_value=self.tdir), mock.patch("sys.stdout", out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_cli_report_and_json(self) -> None:
        self.policy("[metrics.evaluate]\nmin_sessions = 2\nmin_net_saving_tokens = 100\n")
        for d in range(4):
            self.session(d, "claude-haiku-5-5", ['.venv/bin/ctx search "a" "b"', "rg x"])
        code, out = self.run_cli("evaluate")
        self.assertEqual(code, 0)
        for needle in ("VEREDITO GERAL", "Por recurso", "busca e índice", "Recomendação", "Suposições", "estimativa"):
            self.assertIn(needle, out)
        code, out = self.run_cli("evaluate", "--json", "--model", "haiku")
        data = json.loads(out)
        self.assertEqual(data["model_filter"], "haiku")
        self.assertEqual(data["overall"]["sessions"], 4)
        self.assertEqual(self.run_cli("evaluate", "--since", "ontem")[0], 2)

    def test_cli_metrics_shows_ctx_usage_and_pointer_to_evaluate(self) -> None:
        self.policy("")
        start = self.session(0, "claude-haiku-5-5", ['.venv/bin/ctx search "a"', "rg x"])
        self.event(start + 5, cmd="search", latency_ms=11.0)
        self.flush_events()
        code, out = self.run_cli("metrics", "--transcripts", str(self.tdir))
        self.assertEqual(code, 0)
        for needle in ("Uso do ctx por sessão", "ctx search", "Latência por comando", "ctx evaluate"):
            self.assertIn(needle, out)
        self.assertEqual(self.run_cli("metrics", "--transcripts", str(self.tdir), "--since", "x")[0], 2)

    def test_evaluate_command_is_installed_for_prompt_use(self) -> None:
        from ctx.installer import kit_text

        self.assertIn("ctx evaluate", kit_text("ctx-avaliar.md"))
        self.assertIn("{{args}}", kit_text("ctx-avaliar.toml"))


if __name__ == "__main__":
    unittest.main()
