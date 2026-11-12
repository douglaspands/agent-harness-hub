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
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from ctx import BINS_JSON, CTX_DIR, POLICY_PATH, WORKSPACE_ROOT, __version__

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

### 7. Índice saudável, memória, upstream e receitas
- **Busca nunca trava**: `{runner} search` não escreve no índice e para em 3 s (`--timeout`); ao estourar devolve o `rg` equivalente pronto — rode-o. Algo lento ou estranho: `{runner} doctor` e, se falhar, `{runner} doctor --fix`.
- **Escopo**: o índice respeita `.gitignore` e ignora binários, arquivos grandes e diretórios de build/cache/venv; material de referência fora do git entra por `[index.reference]`. Filtre com `{runner} search --scope code|reference|upstream`. Patches (`.patch`/`.diff`) são indexados com arquivos tocados, símbolos e hunks.
- **Sessão retomada ou contexto compactado?** Antes de reabrir arquivos ou repetir comandos, consulte a memória local: `{runner} recall "<pergunta>"` (ex.: "último comando de build que deu certo", "URL usada para o patch X") e `{runner} fact search "<termo>"`. Nada sai da máquina.
- **Registre o que custou caro descobrir**: `{runner} fact add "<fato>" --source arquivo:linha|url --tags a,b`; `{runner} fact stale` lista fatos cuja fonte mudou; `{runner} fact review` revisa candidatos da captura de fim de sessão.
- **Fontes upstream**: `{runner} upstream get <projeto> <versão> <caminho>` baixa uma única vez para `.ctx/upstream/` (cache + índice) e devolve o caminho local; leia-o com a ferramenta de leitura e não baixe de novo à mão.
- **Rotinas de várias etapas**: `{runner} recipe list` e `{runner} recipe run <nome> [args]` (ex.: `copy-verify <origem> <destino>`) fazem tudo numa chamada; cada etapa passa pelo guardrail.
- **Está compensando?** Se o usuário pedir uma avaliação de eficiência/utilidade do ctx (ex.: "avalie a eficiência do ctx", `/ctx-avaliar`), rode `{runner} evaluate` e resuma o veredito por recurso, o ganho líquido estimado e a recomendação, sem inventar números.
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


CAPTURE_MARKER = "recall capture --from-hook"


def _is_guardrail_entry(entry: Any) -> bool:
    """Detecta entradas gerenciadas pelo ctx: guardrail e captura de fim de sessão (formato válido ou legado solto)."""
    if not isinstance(entry, dict):
        return False
    markers = (GUARDRAIL_MARKER, CAPTURE_MARKER)
    if any(m in str(entry.get("command", "")) for m in markers):
        return True
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        m in str(h.get("command", "")) for h in hooks if isinstance(h, dict) for m in markers
    )


def merge_hook_entries(
    existing: dict[str, Any],
    event_entries: dict[str, dict[str, Any]],
    extra: dict[str, Any] | None = None,
    remove: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Mescla as entradas do ctx (uma por evento) preservando as demais chaves e hooks do arquivo.

    Entradas antigas do ctx (inclusive no formato legado inválido) são substituídas; `remove` retira as entradas do
    ctx desses eventos (ex.: captura desligada) sem tocar nas de terceiros.
    """
    settings = {**(extra or {}), **existing}
    hooks = dict(settings.get("hooks") or {})
    for event, entry in event_entries.items():
        others = [e for e in (hooks.get(event) or []) if not _is_guardrail_entry(e)]
        hooks[event] = [entry, *others]
    for event in remove:
        if event in hooks:
            kept = [e for e in (hooks.get(event) or []) if not _is_guardrail_entry(e)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    settings["hooks"] = hooks
    return settings


def capture_hook_command(runner: str) -> str:
    """Comando do hook de parada: `<runner> recall capture --from-hook` (runner relativo parte da raiz do projeto)."""
    first, _, rest = runner.partition(" ")
    if "/" in first and not first.startswith("/"):
        first = f"$CLAUDE_PROJECT_DIR/{first}"
    return f'"{first}"{(" " + rest) if rest else ""} {CAPTURE_MARKER}'


def build_claude_settings(existing: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Claude Code: hooks.PreToolUse[] = {matcher (string), hooks: [{type: "command", command}]}.

    Com `[recall].capture_on_stop = true` registra também o hook `Stop` da captura de candidatos a fato;
    com false remove o que o ctx tinha registrado (hooks de terceiros ficam).
    """
    policy = policy or {}
    entry = {"matcher": "", "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]}
    events = {"PreToolUse": entry}
    remove: tuple[str, ...] = ("Stop",)
    if policy.get("recall", {}).get("capture_on_stop"):
        runner = policy.get("ctx", {}).get("runner", DEFAULT_RUNNER)
        events["Stop"] = {"hooks": [{"type": "command", "command": capture_hook_command(runner), "timeout": 30}]}
        remove = ()
    return merge_hook_entries(existing, events, remove=remove)


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
            builder = HARNESS_BUILDERS[harness]
            content = builder(_read_json(path), policy) if harness == "claude" else builder(_read_json(path))
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


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def fix_index(workspace_root: Path) -> dict[str, Any]:
    """Conserta o índice: checkpoint do WAL, VACUUM e reindex (recria do zero se corrompido ou com WAL gigante)."""
    from ctx.rag import (
        WAL_REBUILD_BYTES,
        checkpoint,
        db_path_for,
        get_db,
        index_state,
        index_workspace,
        remove_db,
        wal_bytes,
    )

    db = db_path_for(workspace_root)
    before = {"db": db.stat().st_size if db.exists() else 0, "wal": wal_bytes(db)}
    steps: list[str] = []
    state = index_state(db)
    if before["wal"] > WAL_REBUILD_BYTES or state == "broken":
        remove_db(db)
        steps.append("índice recriado (WAL gigante ou banco corrompido)")
    else:
        checkpoint(db)
        steps.append("checkpoint do WAL (TRUNCATE)")
        if state == "ok":
            con = get_db(db)
            try:
                con.execute("VACUUM")
            finally:
                con.close()
            steps.append("VACUUM")
    res = index_workspace(workspace_root, force=state != "ok")
    steps.append(f"reindex ({res['indexed']} atualizados, {res['total']} no total)")
    after = {"db": db.stat().st_size if db.exists() else 0, "wal": wal_bytes(db)}
    return {"steps": steps, "before": before, "after": after}


def doctor(workspace_root: Path = WORKSPACE_ROOT, fix: bool = False) -> dict[str, Any]:
    """Diagnóstico completo de integridade dos componentes de governança, hooks e caches.

    Com `fix=True` conserta antes o índice (checkpoint, VACUUM, reindex) e reporta os tamanhos antes/depois.
    """
    start_t = time.perf_counter()
    checks: list[dict[str, Any]] = []
    fix_report = fix_index(workspace_root) if fix else None

    def add_check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    # Checar policy.toml
    p_path = workspace_root / "policy.toml"
    add_check("policy.toml presente", p_path.exists(), str(p_path))

    # Checar guardrail.py
    g_path = workspace_root / "scripts" / "guardrail.py"
    add_check("scripts/guardrail.py presente", g_path.exists(), str(g_path))
    if g_path.exists():
        found = re.search(r'^KIT_VERSION\s*=\s*"([^"]+)"', g_path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
        kit_version = found.group(1) if found else "anterior à v0.4"
        add_check(
            "scripts/guardrail.py na versão do motor",
            kit_version == __version__,
            f"kit {kit_version}, motor {__version__}" + ("" if kit_version == __version__ else " — rode `ctx upgrade`"),
        )
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
    _index_health_checks(workspace_root, db_path, add_check)

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

    result: dict[str, Any] = {
        "status": "healthy" if all_passed else "attention_required",
        "all_passed": all_passed,
        "checks": checks,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    if fix_report:
        result["fix"] = fix_report
    return result


def _index_health_checks(workspace_root: Path, db_path: Path, add_check: Any) -> None:
    """Saúde do índice: WAL, tamanho do banco, atraso vs arquivos/git e latência de uma busca de teste."""
    from ctx.config import load_config
    from ctx.rag import _search_one, index_state, open_readonly, pending_changes, wal_bytes

    if not db_path.exists():
        return
    cfg = load_config(workspace_root)["doctor"]
    wal = wal_bytes(db_path)
    wal_limit = int(cfg["wal_max_mb"]) * 1024 * 1024
    add_check("Índice: WAL consolidado", wal <= wal_limit, f"{_mb(wal)} (limite {cfg['wal_max_mb']} MB) — `ctx doctor --fix` consolida")
    size = db_path.stat().st_size
    db_limit = int(cfg["db_max_mb"]) * 1024 * 1024
    add_check("Índice: tamanho do banco", size <= db_limit, f"{_mb(size)} (limite {cfg['db_max_mb']} MB) — revise [index] ou rode `ctx doctor --fix`")

    state = index_state(db_path)
    if state != "ok":
        add_check("Índice: esquema", state == "outdated", f"{state}: será recriado no próximo uso (`ctx index`)")
        return
    if cfg["stale_check"]:
        pending = pending_changes(workspace_root)
        add_check(
            "Índice: em dia com os arquivos",
            pending == 0,
            "em dia" if pending == 0 else f"{pending} arquivo(s) diferem do índice — rode `ctx index`",
        )
    t0 = time.perf_counter()
    try:
        con = open_readonly(db_path)
        try:
            _search_one(con, workspace_root, "ctx", 5, None, 0, {}, None, t0 + 5)
        finally:
            con.close()
        ms = (time.perf_counter() - t0) * 1000
        add_check("Índice: busca de teste", ms <= float(cfg["search_max_ms"]), f"{ms:.1f} ms (limite {cfg['search_max_ms']} ms)")
    except Exception as e:  # noqa: BLE001 - qualquer falha de leitura reprova o check
        add_check("Índice: busca de teste", False, f"falhou: {e}")
