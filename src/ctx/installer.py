"""Instalador idempotente do kit (`ctx init`) em qualquer projeto.

O código vem do pacote versionado (src/ctx/kit/), nunca de texto copiado de um chat ou de um guia,
então todo projeto recebe exatamente o mesmo guardrail da versão fixada.

Política de escrita:
- scripts/guardrail.py e scripts/venv-python: código do motor; sempre atualizados para a versão do kit.
  O venv-python garante que o hook rode com .venv/bin/python (cria o .venv com uv, ou com o python da
  máquina só para isso quando o uv não existe) — nunca com o python global.
- policy.toml e skills: pontos de customização do projeto; criados se ausentes, mantidos se já existem
  (use --force para sobrescrever).
- .gitignore: só acrescenta as linhas que faltam.
- Hooks e AGENTS.md/CLAUDE.md/GEMINI.md: gerados pelo `ctx rule sync` (mescla, preserva conteúdo do usuário).

100% biblioteca padrão.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from typing import Any

from ctx import __version__
from ctx.policy import DEFAULT_RUNNER, doctor, sync_rules
from ctx.rag import index_workspace

REPO_URL = "https://github.com/douglaspands/agent-harness-hub"
GIT_SPEC = f"git+{REPO_URL}@v{__version__}"
GITIGNORE_LINES = [".venv/", ".ctx/", ".claude/settings.local.json"]
SKILL_TARGETS = (".claude/skills/workspace-ctx/SKILL.md", ".agents/skills/workspace-ctx/SKILL.md")
TEMPLATE_HEADER = "# Exemplo de policy.toml para governança de Agent Harnesses (copie para a raiz do repositório como policy.toml)"


def kit_text(name: str) -> str:
    return (resources.files("ctx") / "kit" / name).read_text(encoding="utf-8")


def install_commands(target: Path, has_uv: bool | None = None) -> list[str]:
    """Comandos atômicos para instalar o kit no .venv do projeto (nunca no python global)."""
    has_uv = shutil.which("uv") is not None if has_uv is None else has_uv
    pkg = f"agent-harness-hub @ {GIT_SPEC}"
    if has_uv and (target / "pyproject.toml").exists():
        return [f'uv add --dev "{pkg}"']
    if has_uv:
        return ["uv venv .venv", f'uv pip install --python .venv/bin/python "{pkg}"']
    # Sem uv: o python da máquina serve APENAS para criar o .venv
    return ["python3 -m venv .venv", f'.venv/bin/python -m pip install "{pkg}"']


def render_policy(target: Path, runner: str) -> str:
    text = kit_text("policy.toml.example")
    text = text.replace(
        TEMPLATE_HEADER,
        f"# policy.toml gerado por `ctx init` (agent-harness-hub v{__version__}): fonte única de governança do harness.",
    )
    text = text.replace('name = "meu-projeto"', f'name = "{target.name}"', 1)
    if runner != DEFAULT_RUNNER:
        text = text.replace(DEFAULT_RUNNER, runner)
    return text


def render_skill(runner: str) -> str:
    text = kit_text("SKILL.md")
    return text if runner == DEFAULT_RUNNER else text.replace(DEFAULT_RUNNER, runner)


def _write(target: Path, rel: str, content: str, overwrite: bool, actions: list[dict[str, str]], executable: bool = False) -> None:
    path = target / rel
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if current == content:
            actions.append({"path": rel, "status": "inalterado"})
            return
        if not overwrite:
            actions.append({"path": rel, "status": "mantido (já existe; --force sobrescreve)"})
            return
        status = "atualizado"
    else:
        status = "criado"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    actions.append({"path": rel, "status": status})


def _merge_gitignore(target: Path, actions: list[dict[str, str]]) -> None:
    path = target / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in present]
    if not missing:
        actions.append({"path": ".gitignore", "status": "inalterado"})
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# agent-harness-hub (ctx): índice local, cache de binários e overrides locais do harness\n"
    path.write_text(existing + block + "\n".join(missing) + "\n", encoding="utf-8")
    actions.append({"path": ".gitignore", "status": f"atualizado (+{', '.join(missing)})"})


def next_steps(target: Path, runner: str, has_uv: bool | None = None) -> list[str]:
    steps: list[str] = []
    if runner == DEFAULT_RUNNER and not (target / ".venv" / "bin" / "ctx").exists():
        cmds = "  →  ".join(install_commands(target, has_uv))
        steps.append(f"Instale o kit no .venv do projeto (cria .venv/bin/ctx): {cmds}")
    elif runner == "ctx":
        steps.append(f"Se o comando ctx ainda não estiver no PATH: uv tool install {GIT_SPEC}")
    steps.append(
        "Revise policy.toml: [harnesses].enabled só com os harnesses que o projeto usa, commands.allow com os comandos "
        "de build/teste/lint e [toolchains] da stack."
    )
    steps.append(f"Depois de editar o policy.toml: {runner} rule sync  e  {runner} doctor")
    steps.append(
        "Reinicie cada harness para carregar o hook: Claude Code (`claude --permission-mode auto`); Codex (abra `/hooks` "
        "e confie no hook do projeto — hooks não confiáveis são ignorados); Gemini CLI (aprove o hook novo quando avisado); "
        "Cursor (o workspace precisa ser confiável)."
    )
    return steps


def init_project(target: Path, runner: str | None = None, force: bool = False, sync: bool = True) -> dict[str, Any]:
    """Instala/atualiza o kit no projeto `target` e devolve um relatório das ações."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"Diretório alvo inexistente: {target}")
    runner = runner or DEFAULT_RUNNER
    actions: list[dict[str, str]] = []

    _write(target, "scripts/guardrail.py", kit_text("guardrail.py"), overwrite=True, actions=actions, executable=True)
    _write(target, "scripts/venv-python", kit_text("venv-python"), overwrite=True, actions=actions, executable=True)
    _write(target, "policy.toml", render_policy(target, runner), overwrite=force, actions=actions)
    skill = render_skill(runner)
    for rel in SKILL_TARGETS:
        _write(target, rel, skill, overwrite=force, actions=actions)
    _merge_gitignore(target, actions)

    result: dict[str, Any] = {"target": str(target), "runner": runner, "version": __version__, "actions": actions}
    if sync:
        result["sync"] = sync_rules(workspace_root=target)
        index_workspace(workspace_root=target)
        result["doctor"] = doctor(workspace_root=target)
    result["next_steps"] = next_steps(target, runner)
    return result
