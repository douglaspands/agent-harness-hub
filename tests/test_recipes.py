"""Receitas de várias etapas: cada etapa passa pelo guardrail; etapa negada aborta tudo antes de executar."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctx.cli import main
from ctx.metrics import guardrail_module
from ctx.recipes import RecipeError, expand_step, list_recipes, run_recipe

ALLOW = 'allow = ["cp", "sync", "sha256sum", "touch", "git status", "ls"]'


def policy(extra: str = "", allow: str = ALLOW) -> str:
    return f"""
[commands]
{allow}

[paths]
deny = [".env"]

{extra}
"""


class RecipeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "origem.txt").write_text("conteudo\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_policy(self, text: str) -> None:
        (self.root / "policy.toml").write_text(text, encoding="utf-8")


class TestCopyVerify(RecipeBase):
    def test_copies_syncs_verifies_in_one_call(self) -> None:
        self.write_policy(policy())
        res = run_recipe(self.root, "copy-verify", ["origem.txt", "destino.txt"])
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual((self.root / "destino.txt").read_text(encoding="utf-8"), "conteudo\n")
        self.assertEqual([s["exit"] for s in res["steps"]], [0, 0, 0, 0, 0])
        self.assertEqual(res["executed"], 5)
        self.assertEqual(res["steps_saved"], 4)  # 5 etapas numa chamada = 4 chamadas a menos

    def test_unlisted_commands_need_explicit_yes_and_nothing_runs_before(self) -> None:
        self.write_policy(policy(allow='allow = ["git status"]'))
        res = run_recipe(self.root, "copy-verify", ["origem.txt", "destino.txt"])
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual({p["decision"] for p in res["pending"]}, {"defer"})
        self.assertFalse((self.root / "destino.txt").exists())
        done = run_recipe(self.root, "copy-verify", ["origem.txt", "destino.txt"], yes=True)
        self.assertEqual(done["status"], "ok")
        self.assertTrue((self.root / "destino.txt").exists())

    def test_hash_mismatch_fails(self) -> None:
        self.write_policy(
            policy(
                '[recipes.cmp]\nparams = ["a", "b"]\nsteps = [\n'
                '  { run = "sha256sum {a}", capture = "ha" },\n  { run = "sha256sum {b}", capture = "hb" },\n'
                '  { check = "equal", a = "ha", b = "hb", message = "diferentes" },\n]\n'
            )
        )
        (self.root / "outro.txt").write_text("diferente\n", encoding="utf-8")
        res = run_recipe(self.root, "cmp", ["origem.txt", "outro.txt"])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["failed_step"], 3)
        self.assertIn("diferentes", res["detail"])

    def test_failing_step_stops_the_rest(self) -> None:
        self.write_policy(policy())
        res = run_recipe(self.root, "copy-verify", ["nao-existe.txt", "destino.txt"])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["failed_step"], 1)
        self.assertEqual(res["executed"], 1)
        self.assertEqual(res["steps_saved"], 0)


class TestGuardrailDeniesAStep(RecipeBase):
    RECIPE = '[recipes.bad]\ndescription = "x"\nparams = ["alvo"]\nsteps = ["touch marcador.txt", "cat {alvo}"]\n'

    def test_denied_step_aborts_before_anything_runs(self) -> None:
        self.write_policy(policy(self.RECIPE, allow='allow = ["touch", "cat"]'))
        (self.root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        res = run_recipe(self.root, "bad", [".env"], yes=True)  # nem --yes libera um deny
        self.assertEqual(res["status"], "denied")
        self.assertEqual(res["failed_step"], 2)
        self.assertIn(".env", res["reason"])
        self.assertEqual(res["executed"], 0)
        self.assertFalse((self.root / "marcador.txt").exists(), "a etapa 1 não pode ter rodado")

    def test_same_recipe_runs_with_a_safe_argument(self) -> None:
        self.write_policy(policy(self.RECIPE, allow='allow = ["touch", "cat"]'))
        res = run_recipe(self.root, "bad", ["origem.txt"])
        self.assertEqual(res["status"], "ok")
        self.assertTrue((self.root / "marcador.txt").exists())

    def test_chaining_inside_a_step_is_denied(self) -> None:
        self.write_policy(policy('[recipes.chain]\nsteps = ["ls ; ls"]\n'))
        # `;` vira token separado pelo shlex? o guardrail recusa encadeamento na string reconstruída
        res = run_recipe(self.root, "chain", [])
        self.assertIn(res["status"], ("denied", "needs_confirmation", "failed"))
        self.assertNotEqual(res["status"], "ok")

    def test_guardrail_root_is_restored(self) -> None:
        g = guardrail_module()
        before = g.WORKSPACE_ROOT
        self.write_policy(policy())
        run_recipe(self.root, "copy-verify", ["origem.txt", "d.txt"])
        self.assertEqual(g.WORKSPACE_ROOT, before)

    def test_agent_cannot_self_approve_with_yes(self) -> None:
        g = guardrail_module()
        decision, reason = g.evaluate_shell_command(".venv/bin/ctx recipe run copy-verify a b --yes", {"commands": {"allow": [".venv/bin/ctx"]}})
        self.assertEqual(decision, "ask")
        self.assertIn("--yes", reason)
        plain, _ = g.evaluate_shell_command(".venv/bin/ctx recipe run copy-verify a b", {"commands": {"allow": [".venv/bin/ctx"]}})
        self.assertEqual(plain, "allow")


class TestInjectionAndValidation(RecipeBase):
    def test_argument_is_one_token_never_a_shell(self) -> None:
        self.assertEqual(expand_step("cp {a} {b}", {"a": "x; rm -rf /", "b": "$(id)"}), ["cp", "x; rm -rf /", "$(id)"])

    def test_option_injection_and_nul_rejected(self) -> None:
        self.write_policy(policy())
        for bad in ("--help", "-r", "a\0b"):
            with self.assertRaises(RecipeError, msg=bad):
                run_recipe(self.root, "copy-verify", [bad, "d.txt"])

    def test_wrong_arity_unknown_recipe_and_placeholder(self) -> None:
        self.write_policy(policy('[recipes.oops]\nparams = ["a"]\nsteps = ["ls {b}"]\n'))
        with self.assertRaisesRegex(RecipeError, "argumentos esperados"):
            run_recipe(self.root, "copy-verify", ["so-um"])
        with self.assertRaisesRegex(RecipeError, "inexistente"):
            run_recipe(self.root, "nope", [])
        with self.assertRaisesRegex(RecipeError, "placeholder"):
            run_recipe(self.root, "oops", ["x"])

    def test_malicious_value_does_not_execute_anything(self) -> None:
        self.write_policy(policy())
        res = run_recipe(self.root, "copy-verify", ["x; touch pwned.txt", "d.txt"])
        self.assertEqual(res["status"], "failed")
        self.assertFalse((self.root / "pwned.txt").exists())


class TestListAndCli(RecipeBase):
    def test_list_has_builtin_and_policy_recipes(self) -> None:
        self.write_policy(policy('[recipes.minha]\ndescription = "faz algo"\nparams = ["x"]\nsteps = ["ls"]\n'))
        rows = {r["name"]: r for r in list_recipes(self.root)}
        self.assertEqual(rows["copy-verify"]["params"], ["origem", "destino"])
        self.assertIn("sha256", rows["copy-verify"]["description"])
        self.assertEqual(rows["minha"]["description"], "faz algo")

    def test_policy_overrides_builtin(self) -> None:
        self.write_policy(policy('[recipes.copy-verify]\ndescription = "minha versão"\nparams = ["a"]\nsteps = ["ls"]\n'))
        self.assertEqual({r["name"]: r for r in list_recipes(self.root)}["copy-verify"]["description"], "minha versão")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("ctx.cli.WORKSPACE_ROOT", self.root), mock.patch("sys.stdout", out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_cli_exit_codes_and_summary(self) -> None:
        self.write_policy(policy())
        code, out = self.run_cli("recipe", "run", "copy-verify", "origem.txt", "d.txt")
        self.assertEqual(code, 0)
        self.assertIn("4 chamadas economizadas", out)
        code, out = self.run_cli("recipe", "list")
        self.assertEqual(code, 0)
        self.assertIn("copy-verify", out)
        self.write_policy(policy(TestGuardrailDeniesAStep.RECIPE, allow='allow = ["touch", "cat"]'))
        (self.root / ".env").write_text("x", encoding="utf-8")
        code, out = self.run_cli("recipe", "run", "bad", ".env")
        self.assertEqual(code, 3)
        self.assertIn("negada pelo guardrail", out)
        self.write_policy(policy(allow='allow = ["git status"]'))
        code, out = self.run_cli("recipe", "run", "copy-verify", "origem.txt", "e.txt")
        self.assertEqual(code, 4)
        self.assertIn("--yes", out)

    def test_cli_logs_event_with_steps_saved(self) -> None:
        self.write_policy(policy())
        self.run_cli("recipe", "run", "copy-verify", "origem.txt", "d.txt")
        from ctx.events import read_events

        ev = [e for e in read_events(self.root) if e["cmd"] == "recipe run"][-1]
        self.assertEqual((ev["recipe"], ev["steps_saved"], ev["status"]), ("copy-verify", 4, "ok"))
        self.assertIn("latency_ms", ev)


if __name__ == "__main__":
    unittest.main()
