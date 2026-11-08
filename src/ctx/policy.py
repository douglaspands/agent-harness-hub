"""Sincronizador declarativo de políticas multi-harness, cache de binários e diagnóstico (doctor).

Lê policy.toml e mantém sincronizados:
- .ctx/bins.json (Cache de binários, toolchains Sandbox-Safe e Receitas One-Shot)
- .agents/hooks.json (Antigravity AGY)
- .claude/settings.json (Claude Code)
- AGENTS.md / CLAUDE.md / GEMINI.md (Instruções canônicas de Turno 0)

100% biblioteca padrão (tomllib, json, shutil, pathlib, time, os, sys).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from ctx import BINS_JSON, CTX_DIR, POLICY_PATH, WORKSPACE_ROOT

DEFAULT_RUNNER = ".venv/bin/ctx"

DEFAULT_PROBE_TOOLS = [
    "git",
    "uv",
    "python3",
    "python",
    "sqlite3",
    "rg",
    "fd",
    "jq",
    "tree",
    "podman",
    "docker",
    "asdf",
    "node",
    "npm",
    "npx",
    "cargo",
    "rustc",
    "go",
]


def load_policy(policy_path: Path = POLICY_PATH) -> dict[str, Any]:
    """Carrega o policy.toml do repositório."""
    if not policy_path.exists():
        return {}
    with open(policy_path, "rb") as f:
        return tomllib.load(f)


def resolve_binaries(workspace_root: Path = WORKSPACE_ROOT, refresh: bool = False) -> dict[str, Any]:
    """Descobre e persiste caminhos absolutos de binários e toolchains em .ctx/bins.json (< 1ms)."""
    start_t = time.perf_counter()
    ctx_dir = workspace_root / ".ctx"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    bins_file = ctx_dir / "bins.json"

    if not refresh and bins_file.exists():
        try:
            cached = json.loads(bins_file.read_text(encoding="utf-8"))
            if "binaries" in cached:
                cached["elapsed_ms"] = round((time.perf_counter() - start_t) * 1000, 2)
                return cached
        except Exception:
            pass

    policy = load_policy(workspace_root / "policy.toml")
    tools_to_check = set(DEFAULT_PROBE_TOOLS)

    # Coletar comandos da whitelist do policy.toml (binários locais como .venv/bin/ctx entram em toolchains)
    for cmd in policy.get("commands", {}).get("allow", []):
        first_token = cmd.split()[0] if cmd.strip() else ""
        if first_token and "/" not in first_token and not first_token.startswith("."):
            tools_to_check.add(first_token)

    # Coletar managers e runners dos toolchains
    for tc_name, tc in policy.get("toolchains", {}).items():
        if "manager" in tc:
            tools_to_check.add(tc["manager"])
        runner_first = tc.get("canonical_runner", "").split()[0]
        if runner_first:
            tools_to_check.add(runner_first)

    # Coletar containers
    for cont_key in ("prefer", "fallback"):
        val = policy.get("containers", {}).get(cont_key)
        if val:
            tools_to_check.add(val)

    resolved: dict[str, dict[str, Any]] = {}
    for tool in sorted(tools_to_check):
        p = shutil.which(tool)
        resolved[tool] = {
            "available": p is not None,
            "path": p,
        }

    # Resolução de shims do asdf se configurado
    asdf_shims_dir = Path.home() / ".asdf" / "shims"
    asdf_shims = {}
    if asdf_shims_dir.exists():
        for item in asdf_shims_dir.iterdir():
            if item.is_file() and os.access(item, os.X_OK):
                asdf_shims[item.name] = str(item)

    # Toolchains Sandbox-Safe: runner canônico + executores locais do .venv
    toolchains: dict[str, dict[str, Any]] = {}
    for tc_name, tc in policy.get("toolchains", {}).items():
        runners = {}
        for runner in tc.get("sandbox_runners", []):
            runner_path = workspace_root / runner
            runners[runner] = {
                "available": runner_path.exists() and os.access(runner_path, os.X_OK),
                "path": str(runner_path),
            }
        toolchains[tc_name] = {
            "canonical_runner": tc.get("canonical_runner", ""),
            "sandbox_runners": runners,
            "forbidden_direct": tc.get("forbidden_direct", []),
        }

    data = {
        "workspace_root": str(workspace_root),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "binaries": resolved,
        "asdf_shims": asdf_shims,
        "toolchains": toolchains,
        "one_shot_recipes": policy.get("commands", {}).get("one_shot_recipes", {}),
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }

    bins_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


FILE_TOOLS_TEXT = {
    "claude": (
        "- **Ler**: ferramenta `Read` (use `offset`/`limit` para trechos indicados pelo `ctx search`). Vários arquivos: várias chamadas `Read` no mesmo turno.\n"
        "- **Editar**: `Edit` (substituição exata). **Criar/reescrever**: `Write`.\n"
        "- **Não use o shell para isso**: nada de `cat`/`head`/`tail`/`sed -n` para ler, nem `sed -i`, `>`, `>>`, heredoc (`<<EOF`) ou `tee` para escrever. "
        "O shell fica para executar programas, testes, git e buscas (`ctx search`, `rg`)."
    ),
    "antigravity": (
        "- **Ler, editar e criar arquivos**: use as ferramentas nativas de arquivo do harness, não o terminal "
        "(Gemini CLI: `read_file`, `replace` para trechos, `write_file` para arquivos novos).\n"
        "- **Não use o shell para isso**: nada de `cat`/`head`/`tail`/`sed -n` para ler, nem `sed -i`, `>`, `>>`, heredoc (`<<EOF`) ou `tee` para escrever. "
        "O terminal fica para executar programas, testes, git e buscas (`ctx search`, `rg`)."
    ),
    "agents": (
        "- **Se o seu harness tem ferramentas nativas de arquivo** (ex.: `Read`/`Edit`/`Write` no Claude Code), use-as para ler, editar e criar arquivos; "
        "não use `cat`/`head`/`tail`/`sed -n` para ler nem `sed -i`, `>`, `>>`, heredoc ou `tee` para escrever.\n"
        "- **Se o harness só tem shell** (ex.: Codex), leia trechos delimitados (`sed -n 'A,Bp' arquivo`, `rg -n -C 2`) em vez de arquivos inteiros "
        "e aplique mudanças pelo mecanismo de patch do harness (ex.: `apply_patch`), não por redirecionamento."
    ),
}


def generate_instructions_markdown(policy: dict[str, Any], bins_data: dict[str, Any], harness: str = "agents") -> str:
    """Gera o bloco padronizado de instruções de Turno 0 (texto de ferramentas de arquivo ajustado ao harness)."""
    binaries = bins_data.get("binaries", {})
    available_bins = [k for k, v in binaries.items() if v.get("available")]

    toolchains = policy.get("toolchains", {})
    tc_lines = []
    for stack, info in toolchains.items():
        runner = info.get("canonical_runner", "")
        sandbox = info.get("sandbox_runners", [])
        forbidden = ", ".join(f"`{f}`" for f in info.get("forbidden_direct", []))
        reason = info.get("reason", "")
        runners = " ou ".join(f"`{r}`" for r in [runner, *sandbox] if r)
        tc_lines.append(f"- **{stack.capitalize()}**: Invoque via {runners}. Proibido: {forbidden}. ({reason})")

    toolchains_doc = "\n".join(tc_lines) if tc_lines else "Nenhum toolchain restritivo configurado."
    bins_summary = ", ".join(f"`{b}`" for b in sorted(available_bins))

    recipes = policy.get("commands", {}).get("one_shot_recipes", {})
    if recipes:
        recipe_rows = "\n".join(f"| `{name}` | `{cmd.replace('|', chr(92) + '|')}` |" for name, cmd in recipes.items())
        recipes_doc = f"| Receita | Comando |\n| :--- | :--- |\n{recipe_rows}"
    else:
        recipes_doc = "Nenhuma Receita One-Shot configurada."

    data_path = policy.get("storage", {}).get("btrfs_nocow", {}).get("path", "~/data")
    runner = policy.get("ctx", {}).get("runner", DEFAULT_RUNNER)
    file_tools_cfg = policy.get("file_tools", {})
    file_tools_doc = FILE_TOOLS_TEXT.get(harness, FILE_TOOLS_TEXT["agents"])
    if file_tools_cfg.get("mode") == "coach" and harness in file_tools_cfg.get("harnesses", ["claude", "antigravity"]):
        file_tools_doc += "\n- O guardrail está em modo `coach`: leitura/escrita de arquivo via shell recebe `deny` com a ferramenta correta."

    return f"""<!-- AUTOGENERATED_BY_CTX_START -->
## 🤖 Diretrizes Canônicas de Autonomia e Descoberta para Agentes (Turno 0)

Este repositório é gerenciado pelo **motor `ctx`** com governança multi-harness (`policy.toml`) e guardrail ativo (`scripts/guardrail.py`).

### 1. Descoberta One-Shot em 1 Turno (< 15ms)
- **NUNCA** execute varreduras cegas (`ls -R`, `find .`, `tree` sem `-L`, `grep -r`) nem leia arquivos inteiros para localizar código ou regras.
- No **Turno 1**, use o motor `ctx` (`{runner}`), em chamadas paralelas no mesmo turno:
  - Estado consolidado (Git + changes OpenSpec + saúde do índice): `{runner} status`.
  - Mapa estrutural (sem `openspec/changes/archive/`): `{runner} map [subcaminhos...]`.
  - Busca multi-query com preview de 25 linhas: `{runner} search "<q1>" "<q2>" --lines 25 [--path src]`.
- Use o retorno `arquivo:inicio-fim` para ler cirurgicamente só as linhas necessárias.

### 2. Cache de Ferramentas, Toolchains Sandbox-Safe e Receitas One-Shot
- **Zero turnos de sondagem**: NÃO rode `which`, `whereis` ou `--version`. Consulte `.ctx/bins.json`.
- Ferramentas pré-verificadas disponíveis no ambiente:
  {bins_summary}
- **Toolchains**:
{toolchains_doc}
- Binários globais proibidos recebem **`deny` imediato**; os executores locais do `.venv` funcionam mesmo em sandbox sem `uv`.
- **Receitas One-Shot** (comandos ineficientes recebem `deny` instrutivo com a receita correta):

{recipes_doc}

### 3. Decisões do Guardrail
- `deny`: segredos (`paths.deny`), padrões destrutivos, encadeamento shell, toolchains proibidos e comandos ineficientes (coaching).
- `ask` (forte): `sudo`, instalação de pacotes, `systemctl start/stop/restart`, `git push`, wrappers (`env`, `xargs`, `sh -c`…), redirecionamento `>`, `tee`, `--privileged`, escrita em arquivos protegidos ou fora do workspace e qualquer acesso a `{data_path}`.
- `defer`: baixo risco fora da whitelist (comandos/filtros não listados, leitura fora do workspace) — o Claude Code segue o próprio modo de permissão; o Antigravity pergunta (`force_ask`).

### 4. Comandos Atômicos (sem encadeamento shell)
- É **proibido** encadear comandos shell com `&&`, `||`, `;`, `&`, subshells `$()` ou backticks: o guardrail recusa (`deny`).
- Execute **um comando por chamada**, para que cada um seja avaliado e auditado sozinho.

### 5. Política Container-First e Armazenamento Btrfs
- **Container-First**: Para utilitários ou bancos de dados temporários (PostgreSQL, Redis, etc.), utilize containers efêmeros rootless (`podman run --rm` ou `docker run --rm`). NUNCA instale pacotes no host sem confirmação explícita.
- **Armazenamento Btrfs No-COW**: Em caso de manipulação de bancos de dados pesados, datasets volumosos ou imagens de VM, pergunte ao usuário se autoriza utilizar `{data_path}` (`chattr +C` / No-COW para evitar fragmentação no Btrfs).

### 6. Ferramentas Nativas de Arquivo
{file_tools_doc}
<!-- AUTOGENERATED_BY_CTX_END -->
"""


GUARDRAIL_MARKER = "scripts/guardrail.py"
# O hook roda sempre com o Python do .venv (scripts/venv-python cria o .venv sob demanda); nunca o python global.
CLAUDE_HOOK_COMMAND = '"$CLAUDE_PROJECT_DIR/scripts/venv-python" "$CLAUDE_PROJECT_DIR/scripts/guardrail.py"'
AGY_HOOK_COMMAND = "scripts/venv-python scripts/guardrail.py"
# Codex não expõe variável com a raiz do projeto e roda o hook no cwd da sessão: resolve pela raiz do git.
CODEX_HOOK_COMMAND = 'root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; "$root/scripts/venv-python" "$root/scripts/guardrail.py"'
# Cursor executa hooks de projeto a partir da raiz do projeto (caminhos relativos à raiz).
CURSOR_HOOK_COMMAND = "scripts/venv-python scripts/guardrail.py"
GEMINI_HOOK_COMMAND = '"$GEMINI_PROJECT_DIR/scripts/venv-python" "$GEMINI_PROJECT_DIR/scripts/guardrail.py"'

ALL_HARNESSES = ("claude", "codex", "cursor", "gemini", "antigravity")
DEFAULT_ENABLED_HARNESSES = ("claude", "antigravity")  # policies antigas sem [harnesses]
HARNESS_CONFIG_FILES = {
    "claude": ".claude/settings.json",
    "codex": ".codex/hooks.json",
    "cursor": ".cursor/hooks.json",
    "gemini": ".gemini/settings.json",
    "antigravity": ".agents/hooks.json",
}


def enabled_harnesses(policy: dict[str, Any]) -> list[str]:
    enabled = policy.get("harnesses", {}).get("enabled", list(DEFAULT_ENABLED_HARNESSES))
    return [h for h in ALL_HARNESSES if h in enabled]


def _is_guardrail_entry(entry: Any) -> bool:
    """Detecta entradas do guardrail (formato válido ou o formato legado inválido com 'command' solto)."""
    if not isinstance(entry, dict):
        return False
    if GUARDRAIL_MARKER in str(entry.get("command", "")):
        return True
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(GUARDRAIL_MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def merge_hook_entries(existing: dict[str, Any], event_entries: dict[str, dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mescla as entradas do guardrail (uma por evento) preservando as demais chaves e hooks do arquivo.

    Entradas antigas do guardrail (inclusive no formato legado inválido) são substituídas.
    """
    settings = {**(extra or {}), **existing}
    hooks = dict(settings.get("hooks") or {})
    for event, entry in event_entries.items():
        others = [e for e in (hooks.get(event) or []) if not _is_guardrail_entry(e)]
        hooks[event] = [entry, *others]
    settings["hooks"] = hooks
    return settings


def build_claude_settings(existing: dict[str, Any]) -> dict[str, Any]:
    """Claude Code: hooks.PreToolUse[] = {matcher (string), hooks: [{type: "command", command}]}."""
    entry = {"matcher": "", "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]}
    return merge_hook_entries(existing, {"PreToolUse": entry})


def build_codex_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    """Codex: mesmo formato do Claude; matcher regex cobrindo shell (Bash) e edições (apply_patch)."""
    entry = {
        "matcher": "^(Bash|apply_patch)$",
        "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 30, "statusMessage": "agent-harness-hub guardrail"}],
    }
    return merge_hook_entries(existing, {"PreToolUse": entry})


def build_cursor_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    """Cursor: {version: 1, hooks: {beforeShellExecution: [{command, timeout}], beforeReadFile: [...]}}."""
    entry = {"command": CURSOR_HOOK_COMMAND, "timeout": 30}
    return merge_hook_entries(existing, {"beforeShellExecution": dict(entry), "beforeReadFile": dict(entry)}, extra={"version": 1})


def build_gemini_settings(existing: dict[str, Any]) -> dict[str, Any]:
    """Gemini CLI: hooks.BeforeTool[] = {hooks: [{name, type: "command", command, timeout (ms)}]} (sem matcher = todas)."""
    entry = {
        "hooks": [
            {"name": "agent-harness-hub-guardrail", "type": "command", "command": GEMINI_HOOK_COMMAND, "timeout": 30000}
        ]
    }
    return merge_hook_entries(existing, {"BeforeTool": entry})


def validate_hook_groups(settings: dict[str, Any], event: str) -> tuple[bool, str]:
    """Valida o formato {matcher?, hooks: [{type: command, command}]} (Claude, Codex, Gemini) e a presença do guardrail."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False, "'hooks' deve ser um objeto"
    found = False
    for ev, entries in hooks.items():
        if not isinstance(entries, list):
            return False, f"hooks.{ev} deve ser uma lista"
        for i, entry in enumerate(entries):
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list):
                return False, f"hooks.{ev}.{i}.hooks ausente (formato legado inválido) — rode `ctx rule sync`"
            if "matcher" in entry and not isinstance(entry["matcher"], str):
                return False, f"hooks.{ev}.{i}.matcher deve ser string"
            for j, h in enumerate(inner):
                if not isinstance(h, dict) or h.get("type") != "command" or not h.get("command"):
                    return False, f"hooks.{ev}.{i}.hooks.{j} precisa de type='command' e command"
                if ev == event and GUARDRAIL_MARKER in h["command"]:
                    found = True
    if not found:
        return False, f"Hook {event} do guardrail não registrado"
    return True, f"{event} -> scripts/guardrail.py (formato válido)"


def validate_claude_hooks(settings: dict[str, Any]) -> tuple[bool, str]:
    return validate_hook_groups(settings, "PreToolUse")


def validate_cursor_hooks(settings: dict[str, Any]) -> tuple[bool, str]:
    if settings.get("version") != 1:
        return False, "'version' deve ser 1"
    hooks = settings.get("hooks", {})
    for event in ("beforeShellExecution", "beforeReadFile"):
        entries = hooks.get(event)
        if not isinstance(entries, list) or not any(
            isinstance(e, dict) and GUARDRAIL_MARKER in str(e.get("command", "")) for e in entries
        ):
            return False, f"Hook {event} do guardrail não registrado"
    return True, "beforeShellExecution/beforeReadFile -> scripts/guardrail.py (formato válido)"


HARNESS_BUILDERS = {
    "claude": build_claude_settings,
    "codex": build_codex_hooks,
    "cursor": build_cursor_hooks,
    "gemini": build_gemini_settings,
}
HARNESS_VALIDATORS = {
    "claude": validate_claude_hooks,
    "codex": lambda s: validate_hook_groups(s, "PreToolUse"),
    "cursor": validate_cursor_hooks,
    "gemini": lambda s: validate_hook_groups(s, "BeforeTool"),
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def sync_rules(workspace_root: Path = WORKSPACE_ROOT) -> dict[str, Any]:
    """Sincroniza policy.toml para todos os harnesses (.agents, .claude, AGENTS.md, CLAUDE.md, GEMINI.md)."""
    start_t = time.perf_counter()
    policy = load_policy(workspace_root / "policy.toml")
    bins_data = resolve_binaries(workspace_root=workspace_root, refresh=True)

    synced: list[str] = [".ctx/bins.json"]

    # 1. Hooks por harness habilitado em [harnesses].enabled (mescla: preserva chaves e hooks de terceiros)
    for harness in enabled_harnesses(policy):
        rel = HARNESS_CONFIG_FILES[harness]
        path = workspace_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if harness == "antigravity":
            # Formato do AGY ainda não validado na documentação oficial: mantido como na v0.1.
            content: dict[str, Any] = {"hooks": {"PreToolUse": [{"command": AGY_HOOK_COMMAND}]}}
        else:
            content = HARNESS_BUILDERS[harness](_read_json(path))
        path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        synced.append(rel)

    # 3. Atualizar AGENTS.md, CLAUDE.md e GEMINI.md (texto de ferramentas de arquivo por harness)
    for doc_name, harness in (("AGENTS.md", "agents"), ("CLAUDE.md", "claude"), ("GEMINI.md", "antigravity")):
        instructions_content = generate_instructions_markdown(policy, bins_data, harness=harness)
        doc_path = workspace_root / doc_name
        if doc_path.exists():
            existing = doc_path.read_text(encoding="utf-8")
            start_tag = "<!-- AUTOGENERATED_BY_CTX_START -->"
            end_tag = "<!-- AUTOGENERATED_BY_CTX_END -->"
            if start_tag in existing and end_tag in existing:
                before = existing.split(start_tag)[0]
                after = existing.split(end_tag)[1]
                new_text = before + instructions_content.strip() + after
            else:
                new_text = instructions_content.strip() + "\n\n" + existing
        else:
            new_text = instructions_content.strip() + "\n"
        doc_path.write_text(new_text, encoding="utf-8")
        synced.append(doc_name)

    elapsed_ms = (time.perf_counter() - start_t) * 1000
    return {
        "status": "success",
        "synced_targets": synced,
        "harnesses": enabled_harnesses(policy),
        "elapsed_ms": round(elapsed_ms, 2),
    }


def doctor(workspace_root: Path = WORKSPACE_ROOT) -> dict[str, Any]:
    """Diagnóstico completo de integridade dos componentes de governança, hooks e caches."""
    start_t = time.perf_counter()
    checks: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    # Checar policy.toml
    p_path = workspace_root / "policy.toml"
    add_check("policy.toml presente", p_path.exists(), str(p_path))

    # Checar guardrail.py
    g_path = workspace_root / "scripts" / "guardrail.py"
    add_check("scripts/guardrail.py presente", g_path.exists(), str(g_path))
    vp_path = workspace_root / "scripts" / "venv-python"
    add_check(
        "scripts/venv-python executável",
        vp_path.exists() and os.access(vp_path, os.X_OK),
        "launcher do hook (Python sempre do .venv)",
    )

    # Checar .ctx/bins.json
    b_path = workspace_root / ".ctx" / "bins.json"
    add_check(".ctx/bins.json presente", b_path.exists(), str(b_path))

    # Checar .ctx/index.db
    db_path = workspace_root / ".ctx" / "index.db"
    add_check(".ctx/index.db presente", db_path.exists(), str(db_path))

    # Checar hooks dos harnesses habilitados
    labels = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI", "antigravity": "Antigravity"}
    for harness in enabled_harnesses(load_policy(p_path)):
        rel = HARNESS_CONFIG_FILES[harness]
        cfg_file = workspace_root / rel
        if not cfg_file.exists():
            add_check(f"Hook {labels[harness]} válido", False, f"{rel} ausente — rode `ctx rule sync`")
            continue
        if harness == "antigravity":
            add_check(f"Hook {labels[harness]} presente", True, f"{rel} (formato não validado na doc oficial)")
            continue
        try:
            ok, detail = HARNESS_VALIDATORS[harness](json.loads(cfg_file.read_text(encoding="utf-8")))
        except ValueError as e:
            ok, detail = False, f"JSON inválido: {e}"
        add_check(f"Hook {labels[harness]} válido", ok, f"{rel}: {detail}")

    # Checar documentos de turno 0
    for doc in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        doc_file = workspace_root / doc
        add_check(f"{doc} presente", doc_file.exists(), str(doc_file))

    # Checar python runtime
    add_check("Python 3.11+ disponível", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")

    all_passed = all(c["passed"] for c in checks)
    elapsed_ms = (time.perf_counter() - start_t) * 1000

    return {
        "status": "healthy" if all_passed else "attention_required",
        "all_passed": all_passed,
        "checks": checks,
        "elapsed_ms": round(elapsed_ms, 2),
    }
