"""Instalador idempotente do kit (`ctx init`) em qualquer projeto.

O código vem do pacote instalado a partir da branch main (src/ctx/kit/), nunca de texto copiado de um
chat ou de um guia, então todo projeto recebe exatamente o guardrail da main no momento da instalação.

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

import hashlib
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

from ctx import GIT_SPEC, __version__
from ctx.config import load_config, load_raw_policy
from ctx.policy import DEFAULT_RUNNER, doctor, enabled_harnesses, sync_rules
from ctx.policy_upgrade import upgrade_policy
from ctx.rag import index_workspace, repair_bloat

GITIGNORE_LINES = [".venv/", ".ctx/", ".claude/settings.local.json"]
SKILL_TARGETS = (".claude/skills/workspace-ctx/SKILL.md", ".agents/skills/workspace-ctx/SKILL.md")
SKILL_START = "<!-- CTX_SKILL_START"
SKILL_END = "<!-- CTX_SKILL_END -->"
# sha256 das versões do SKILL.md publicadas ANTES do bloco gerenciado (v0.1/v0.2/v0.3), instaladas com o runner
# padrão (`.venv/bin/ctx`) ou com `ctx` (uv tool). Uma skill idêntica a uma delas nunca foi customizada e pode ser
# substituída por inteiro; qualquer outra (inclusive com runner diferente desses) é tratada como customizada.
LEGACY_SKILL_SHA256 = {
    "67e114cb308631ead1ee4b29e089d9d85e63522e8d6ec5134e5867842b664480",
    "d4e7962137a1cdad8d33887ed3ab82507f8be4bba9433df0e1a1ba762e0a1c16",
    "65a8302f0feaca172614e2d6a14764759289b83ea3daa2fe5d780a7fd03678e6",
}
# Comandos de prompt gerenciados pelo kit (sempre atualizados, como o guardrail): arquivo do kit -> destino
COMMAND_TARGETS = {
    "claude": ("ctx-avaliar.md", ".claude/commands/ctx-avaliar.md"),
    "gemini": ("ctx-avaliar.toml", ".gemini/commands/ctx-avaliar.toml"),
}
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


def upgrade_skill_text(existing: str, new: str) -> tuple[str, str]:
    """Atualiza uma skill instalada sem perder customizações. Retorna (texto final, status).

    1. com bloco gerenciado (CTX_SKILL_START/END): troca só o bloco;
    2. sem marcadores e idêntica a uma versão publicada do kit: substitui por inteiro;
    3. sem marcadores e customizada: preserva o conteúdo do usuário e insere o bloco gerenciado após o frontmatter.
    """
    if existing == new:
        return existing, "inalterado"
    if SKILL_START in existing and SKILL_END in existing:
        block = new[new.index(SKILL_START) : new.index(SKILL_END) + len(SKILL_END)]
        a, b = existing.index(SKILL_START), existing.index(SKILL_END) + len(SKILL_END)
        merged = existing[:a] + block + existing[b:]
        return merged, ("inalterado" if merged == existing else "atualizado (bloco gerenciado)")
    if hashlib.sha256(existing.encode()).hexdigest() in LEGACY_SKILL_SHA256:
        return new, "atualizado (versão anterior do kit)"
    block = new[new.index(SKILL_START) : new.index(SKILL_END) + len(SKILL_END)]
    if existing.startswith("---\n") and "\n---\n" in existing[4:]:
        cut = existing.index("\n---\n", 4) + len("\n---\n")
        merged = existing[:cut] + block + "\n\n" + existing[cut:].lstrip("\n")
    else:
        merged = new.rstrip("\n") + "\n\n" + existing
    return merged, "migrado (conteúdo próprio preservado; bloco do kit inserido — remova duplicatas antigas)"


def _write(
    target: Path,
    rel: str,
    content: str,
    overwrite: bool,
    actions: list[dict[str, str]],
    executable: bool = False,
    dry_run: bool = False,
    merge: Any = None,
) -> None:
    """Escreve `content` em `rel`. `merge(existing) -> (texto, status)` troca a política "mantém se existe"."""
    path = target / rel
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if merge is not None and not overwrite:
            content, status = merge(current)
            if content == current:
                actions.append({"path": rel, "status": "inalterado"})
                return
        elif current == content:
            actions.append({"path": rel, "status": "inalterado"})
            return
        elif not overwrite:
            actions.append({"path": rel, "status": "mantido (já existe; --force sobrescreve)"})
            return
        else:
            status = "atualizado"
    else:
        status = "criado"
    if dry_run:
        actions.append({"path": rel, "status": f"atualizaria: {status}"})
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    actions.append({"path": rel, "status": status})


def _merge_gitignore(target: Path, actions: list[dict[str, str]]) -> None:
    path = target / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    # `.venv` e `.venv/` (e `/.venv`) são equivalentes: não duplica o que o projeto já ignora
    present = {line.strip().lstrip("/").rstrip("/") for line in existing.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line.rstrip("/") not in present]
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
        steps.append(f"Instale o pacote Autonomia e Segurança no .venv do projeto (cria .venv/bin/ctx): {cmds}")
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


def init_project(
    target: Path, runner: str | None = None, force: bool = False, sync: bool = True, dry_run: bool = False
) -> dict[str, Any]:
    """Instala/atualiza o kit no projeto `target` e devolve um relatório das ações.

    Serve tanto para a primeira instalação quanto para atualizar um projeto com versão anterior (`ctx upgrade`):
    é idempotente e não destrutivo. Código do motor (guardrail, venv-python, comandos de prompt) é sempre
    atualizado; policy.toml ganha só as seções novas (comentadas); a skill troca apenas o bloco gerenciado;
    o índice (cache) é migrado/recriado. `dry_run=True` só reporta o que mudaria.
    """
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"Diretório alvo inexistente: {target}")
    existing_runner = load_raw_policy(target).get("ctx", {}).get("runner")
    runner = runner or existing_runner or DEFAULT_RUNNER
    actions: list[dict[str, str]] = []

    _write(target, "scripts/guardrail.py", kit_text("guardrail.py"), overwrite=True, actions=actions, executable=True, dry_run=dry_run)
    _write(target, "scripts/venv-python", kit_text("venv-python"), overwrite=True, actions=actions, executable=True, dry_run=dry_run)
    _write(target, "policy.toml", render_policy(target, runner), overwrite=force, actions=actions, dry_run=dry_run)
    policy_up = upgrade_policy(target, __version__, dry_run=dry_run)
    if policy_up["added"]:
        actions.append({"path": "policy.toml", "status": policy_up["status"]})

    skill = render_skill(runner)
    for rel in SKILL_TARGETS:
        _write(
            target, rel, skill, overwrite=force, actions=actions, dry_run=dry_run,
            merge=lambda current: upgrade_skill_text(current, skill),
        )
    enabled = enabled_harnesses(load_raw_policy(target)) if (target / "policy.toml").exists() else ["claude", "antigravity"]
    for harness, (src, dest) in COMMAND_TARGETS.items():
        if harness in enabled:
            text = kit_text(src)
            text = text if runner == DEFAULT_RUNNER else text.replace(DEFAULT_RUNNER, runner)
            _write(target, dest, text, overwrite=True, actions=actions, dry_run=dry_run)
    if not dry_run:
        _merge_gitignore(target, actions)

    result: dict[str, Any] = {
        "target": str(target),
        "runner": runner,
        "version": __version__,
        "actions": actions,
        "policy": policy_up,
        "dry_run": dry_run,
    }
    if sync and not dry_run:
        result["sync"] = sync_rules(workspace_root=target)
        # Índice de versão anterior: WAL inchado é tratado ANTES de abrir; esquema antigo é recriado no get_db.
        result["index_warnings"] = repair_bloat(target, load_config(target))
        index_workspace(workspace_root=target)
        result["doctor"] = doctor(workspace_root=target)
    result["next_steps"] = next_steps(target, runner)
    return result
