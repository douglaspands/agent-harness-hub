"""Receitas de várias etapas (`ctx recipe run <nome> [args]`): uma chamada em vez de uma por etapa.

Declaradas no `policy.toml` (`[recipes.<nome>]`) ou embutidas e genéricas (`copy-verify`). Cada etapa:
- é tokenizada ANTES da substituição dos parâmetros e os valores entram como um único token (nunca passam por um shell,
  então não há injeção de comandos; valores que começam com `-` ou têm NUL são recusados);
- passa pelo guardrail (`evaluate_shell_command`, o mesmo do hook) ANTES de qualquer execução: se uma etapa for
  `deny`, nada roda; `ask`/`defer` só rodam com `--yes`, e o próprio guardrail pede confirmação do usuário para
  `ctx recipe run ... --yes`, então o agente não aprova sozinho o que o guardrail não liberou;
- roda sem shell, com timeout, e a saída volta resumida (código, duração, últimas linhas).

Formato de etapa: string (comando) ou tabela `{run, capture}` (guarda a 1ª palavra da saída), `{check = "equal", a, b}`.
`requires = ["cp", ...]` declara os comandos de que a receita precisa: `ctx recipe list` mostra os que faltam em
`commands.allow`, e o `ctx init` já coloca os das receitas embutidas no `allow` gerado.

100% biblioteca padrão.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ctx.config import load_config, load_raw_policy
from ctx.metrics import guardrail_module

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
TAIL_LINES = 3
TAIL_CHARS = 200


class RecipeError(Exception):
    """Receita inexistente, malformada ou chamada com argumentos inválidos."""


def load_recipes(workspace_root: Path) -> dict[str, dict[str, Any]]:
    recipes = load_config(workspace_root).get("recipes", {})
    return {name: r for name, r in recipes.items() if isinstance(r, dict)}


def missing_requires(recipe: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Comandos que a receita declara em `requires` e que `commands.allow` não libera (M-07)."""
    allow = [str(a) for a in policy.get("commands", {}).get("allow", [])]
    return [
        req for req in recipe.get("requires", [])
        if not any(req == a or req.startswith(a + " ") for a in allow)
    ]


def list_recipes(workspace_root: Path) -> list[dict[str, Any]]:
    policy = load_raw_policy(workspace_root)
    return [
        {
            "name": name,
            "description": r.get("description", ""),
            "params": list(r.get("params", [])),
            "steps": len(r.get("steps", [])),
            "requires": list(r.get("requires", [])),
            "missing": missing_requires(r, policy),
        }
        for name, r in sorted(load_recipes(workspace_root).items())
    ]


def _step_fields(step: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(step, str):
        return "run", {"run": step}
    if isinstance(step, dict) and "run" in step:
        return "run", step
    if isinstance(step, dict) and step.get("check") == "equal":
        return "check", step
    raise RecipeError(f"etapa inválida: {step!r} (use string, {{run, capture}} ou {{check = \"equal\", a, b}})")


def _validate_args(recipe: dict[str, Any], args: list[str]) -> dict[str, str]:
    params = list(recipe.get("params", []))
    if len(args) != len(params):
        usage = " ".join(f"<{p}>" for p in params)
        raise RecipeError(f"argumentos esperados: {usage or '(nenhum)'}; recebidos {len(args)}")
    values = dict(zip(params, args))
    for name, value in values.items():
        if "\0" in value or value.startswith("-"):
            raise RecipeError(f"valor inválido para '{name}': {value!r} (não pode começar com '-'; use ./ para caminhos relativos)")
    return values


def expand_step(template: str, values: dict[str, str]) -> list[str]:
    """Tokeniza o modelo e substitui `{param}` DENTRO de cada token (valores nunca são re-tokenizados)."""
    try:
        tokens = shlex.split(template)
    except ValueError as e:
        raise RecipeError(f"etapa com aspas inválidas: {template!r} ({e})") from e
    out: list[str] = []
    for tok in tokens:
        for name in PLACEHOLDER_RE.findall(tok):
            if name not in values:
                raise RecipeError(f"placeholder {{{name}}} não está em params: {template!r}")
        out.append(PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], tok))
    if not out:
        raise RecipeError("etapa vazia")
    return out


def preflight(workspace_root: Path, recipe: dict[str, Any], values: dict[str, str]) -> list[dict[str, Any]]:
    """Avalia TODAS as etapas de comando no guardrail sem executar nada."""
    g = guardrail_module()
    policy = load_raw_policy(workspace_root)
    previous_root = g.WORKSPACE_ROOT
    g.WORKSPACE_ROOT = workspace_root.resolve()  # o módulo do kit calcula a raiz pelo próprio caminho
    try:
        plan: list[dict[str, Any]] = []
        for i, raw in enumerate(recipe.get("steps", []), start=1):
            kind, fields = _step_fields(raw)
            if kind == "check":
                plan.append({"n": i, "kind": "check", "fields": fields, "decision": "allow", "reason": ""})
                continue
            argv = expand_step(fields["run"], values)
            decision, reason = g.evaluate_shell_command(shlex.join(argv), policy, cwd=workspace_root, harness="recipe")
            plan.append({"n": i, "kind": "run", "argv": argv, "fields": fields, "decision": decision, "reason": reason})
        return plan
    finally:
        g.WORKSPACE_ROOT = previous_root


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()][-TAIL_LINES:]
    return "\n".join(ln[:TAIL_CHARS] for ln in lines)


def run_recipe(
    workspace_root: Path, name: str, args: list[str], yes: bool = False, step_timeout: float = 120.0
) -> dict[str, Any]:
    """Executa a receita. `status`: ok | failed | denied | needs_confirmation."""
    recipes = load_recipes(workspace_root)
    if name not in recipes:
        raise RecipeError(f"receita '{name}' inexistente (disponíveis: {', '.join(sorted(recipes)) or 'nenhuma'})")
    recipe = recipes[name]
    values = _validate_args(recipe, args)
    plan = preflight(workspace_root, recipe, values)
    total = len(plan)
    base: dict[str, Any] = {"recipe": name, "total_steps": total, "steps": [], "steps_saved": 0}

    denied = next((p for p in plan if p["decision"] == "deny"), None)
    if denied:
        return {**base, "status": "denied", "failed_step": denied["n"], "reason": denied["reason"],
                "command": shlex.join(denied["argv"]), "executed": 0}
    pending = [p for p in plan if p["decision"] in ("ask", "defer")]
    if pending and not yes:
        return {
            **base,
            "status": "needs_confirmation",
            "executed": 0,
            "pending": [{"step": p["n"], "command": shlex.join(p["argv"]), "decision": p["decision"], "reason": p["reason"]} for p in pending],
            "missing_requires": missing_requires(recipe, load_raw_policy(workspace_root)),
        }

    captures: dict[str, str] = {}
    for p in plan:
        started = time.perf_counter()
        if p["kind"] == "check":
            f = p["fields"]
            a, b = captures.get(f["a"]), captures.get(f["b"])
            ok = a is not None and a == b
            base["steps"].append({"n": p["n"], "command": f"check {f['a']} == {f['b']}", "exit": 0 if ok else 1, "ms": 0,
                                  "output": "" if ok else f.get("message", "valores diferentes")})
            if not ok:
                return _finish(base, "failed", p["n"], f.get("message", "check falhou"))
            continue
        try:
            proc = subprocess.run(p["argv"], cwd=workspace_root, capture_output=True, text=True, timeout=step_timeout, check=False)
            code, out = proc.returncode, (proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
        except subprocess.TimeoutExpired:
            code, out = 124, f"timeout após {step_timeout:g}s"
        except OSError as e:
            code, out = 127, str(e)
        ms = round((time.perf_counter() - started) * 1000, 1)
        base["steps"].append({"n": p["n"], "command": shlex.join(p["argv"]), "exit": code, "ms": ms, "output": _tail(out)})
        if code != 0:
            return _finish(base, "failed", p["n"], _tail(out) or f"saiu com código {code}")
        cap = p["fields"].get("capture")
        if cap:
            words = out.split()
            captures[cap] = words[0] if words else ""
    return _finish(base, "ok", None, base["steps"][-1]["output"] if base["steps"] else "")


def _finish(result: dict[str, Any], status: str, failed_step: int | None, detail: str) -> dict[str, Any]:
    executed = len(result["steps"])
    result.update({"status": status, "failed_step": failed_step, "detail": detail, "executed": executed})
    # Chamadas economizadas: o agente faria 1 chamada por etapa executada; a receita faz 1 no total.
    result["steps_saved"] = max(0, executed - 1)
    return result
