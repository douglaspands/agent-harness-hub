#!/usr/bin/env python3
"""Hook universal de pré-execução de ferramentas (< 5ms) para governança e segurança.

Compatível com (detecção automática pelo formato do evento):
- Claude Code  (.claude/settings.json  · PreToolUse        · hookSpecificOutput.permissionDecision)
- Codex        (.codex/hooks.json      · PreToolUse        · hookSpecificOutput; Bash e apply_patch; sem "ask")
- Cursor       (.cursor/hooks.json     · beforeShellExecution / beforeReadFile · permission allow|deny|ask)
- Gemini CLI   (.gemini/settings.json  · BeforeTool        · decision allow|deny; sem "ask")
- Google Antigravity (.agents/hooks.json · decision allow|force_ask|deny)

Decisões internas:
- allow : autonomia total (operação segura dentro do escopo).
- ask   : "ask forte" para operações sensíveis (sudo, instalação, git push, escrita protegida/externa, ~/data...).
- deny  : bloqueio imediato (segredos, padrões destrutivos, encadeamento shell, toolchains proibidos,
          coaching One-Shot de comandos ineficientes).
- defer : baixo risco sem aprovação explícita; o Claude Code segue o próprio modo de permissão
          (resposta vazia `{}`), e o Antigravity recebe `force_ask`.

100% biblioteca padrão do Python 3.11+ (zero dependências externas).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = WORKSPACE_ROOT / "policy.toml"

DEFAULT_HARNESS_ALLOW = [
    "/tmp/claude-{uid}/**",
    "~/.claude/projects/**",
    "~/.claude/plans/**",
]
DEFAULT_WRAPPERS = ["env", "xargs", "sh", "bash", "zsh", "nohup", "timeout", "nice", "exec", "eval", "command", "busybox"]
PRIVILEGE_ESCALATION = {"sudo", "doas", "su", "pkexec", "run0"}
CHAIN_TOKENS = {"&&", "||", ";", ";;", "&", ";&", "&;", "|&", "(", ")"}
HARMLESS_REDIRECT_RE = re.compile(r"\d?>&\d|&>\s*/dev/null|\d?>{1,2}\s*/dev/null")
PUNCTUATION = set("();<>|&")


def load_policy() -> dict[str, Any]:
    """Carrega as regras do policy.toml."""
    if not POLICY_PATH.exists():
        return {}
    try:
        with open(POLICY_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


HARNESSES = ("claude", "codex", "cursor", "gemini", "antigravity")
# Harnesses cujo hook NÃO suporta "ask": um valor não suportado faria o hook falhar e a ação seguir (fail-open).
NO_ASK_SUPPORT = {"codex", "gemini"}
CURSOR_EVENTS = {"beforeShellExecution", "beforeReadFile", "beforeMCPExecution", "preToolUse"}


def detect_harness(payload: dict[str, Any]) -> str:
    """Identifica o harness pelo formato do evento recebido no stdin."""
    event = payload.get("hook_event_name")
    if event == "BeforeTool":
        return "gemini"
    if "cursor_version" in payload or event in CURSOR_EVENTS:
        return "cursor"
    if event == "PreToolUse":
        return "codex" if "turn_id" in payload else "claude"
    return "antigravity"


def _hook_specific(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def format_response(harness: str | bool, decision: str, reason: str, ask_fallback: str = "deny") -> dict[str, Any]:
    """Formata a resposta estrita para o protocolo do harness.

    `harness` aceita o nome do harness ou, por compatibilidade, um bool (True = Antigravity, False = Claude Code).
    `ask_fallback` define o que fazer com "ask" em harnesses sem suporte a ask (codex, gemini): "deny" (padrão,
    seguro: o agente recebe o motivo e pede ao usuário) ou "allow".
    """
    if isinstance(harness, bool):
        harness = "antigravity" if harness else "claude"

    if decision == "ask" and harness in NO_ASK_SUPPORT:
        if ask_fallback == "allow":
            decision = "allow"
        else:
            decision = "deny"
            reason = (
                f"Requer confirmação do usuário: {reason} Este harness não suporta 'ask' em hooks; "
                "explique ao usuário o que pretende fazer e peça que ele execute ou aprove manualmente."
            )

    if harness == "antigravity":
        # AGY protojson: "allow", "force_ask", "deny" (defer preserva o comportamento original: force_ask)
        return {"decision": "force_ask" if decision in ("ask", "defer") else decision, "reason": reason}

    if harness == "cursor":
        # Cursor: permission allow|deny|ask (sem modo "seguir o padrão": defer vira ask)
        permission = "ask" if decision == "defer" else decision
        out: dict[str, Any] = {"permission": permission}
        if permission != "allow":
            out.update({"user_message": reason, "agent_message": reason})
        return out

    if harness == "gemini":
        if decision in ("allow", "defer"):
            return {} if decision == "defer" else {"decision": "allow"}
        return {"decision": "deny", "reason": reason}

    # claude e codex: hookSpecificOutput.permissionDecision
    if decision == "defer":
        return {}  # sem decisão: o harness segue o próprio modo de permissão
    if harness == "codex" and decision == "allow":
        return {}  # o Codex só documenta "allow" junto com updatedInput; saída vazia = seguir
    return _hook_specific(decision, reason)


# --------------------------------------------------------------------------------------
# Tokenização shell (ciente de aspas)
# --------------------------------------------------------------------------------------


def tokenize(cmd: str) -> list[str] | None:
    """Divide o comando em tokens respeitando aspas e separando operadores. None se não analisável."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


def is_operator(token: str) -> bool:
    return bool(token) and all(ch in PUNCTUATION for ch in token)


def split_pipeline(tokens: list[str]) -> list[list[str]]:
    """Separa os tokens em segmentos de pipe simples '|'."""
    segments: list[list[str]] = [[]]
    for t in tokens:
        if t == "|":
            segments.append([])
        else:
            segments[-1].append(t)
    return segments


def has_chaining_operators(cmd: str) -> bool:
    """Verifica se há encadeamento não-atômico no shell (&&, ||, ;, &, subshells)."""
    if "\n" in cmd or "\r" in cmd or "$(" in cmd or "`" in cmd:
        return True

    tokens = tokenize(HARMLESS_REDIRECT_RE.sub(" ", cmd))
    if tokens is None:
        # Fallback conservador quando as aspas não fecham
        return bool(re.search(r"(?<!\\)(&&|\|\||;|(?<![>&])&(?![>&]))", cmd))
    return any(t in CHAIN_TOKENS for t in tokens)


# --------------------------------------------------------------------------------------
# Caminhos
# --------------------------------------------------------------------------------------


def resolve_path(path_str: str, cwd: Path | None = None) -> Path:
    p = Path(os.path.expanduser(path_str))
    if not p.is_absolute():
        p = (cwd or WORKSPACE_ROOT) / p
    return p.resolve()


def is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def btrfs_path(policy: dict[str, Any]) -> Path:
    raw = policy.get("storage", {}).get("btrfs_nocow", {}).get("path", "~/data")
    return Path(os.path.expanduser(raw)).resolve()


def matches_deny_path(path: Path, raw_token: str, policy: dict[str, Any]) -> str | None:
    """Retorna a entrada de paths.deny que casa com o caminho, se houver."""
    for dp in policy.get("paths", {}).get("deny", []):
        if "/" not in dp and not dp.startswith("~"):
            # Entrada por nome de arquivo (ex.: .env): casa em qualquer diretório
            if path.name == dp or Path(raw_token).name == dp:
                return dp
            continue
        dp_exp = resolve_path(dp)
        if is_within(path, dp_exp):
            return dp
    return None


def matches_harness_allow(path: Path, policy: dict[str, Any]) -> bool:
    patterns = policy.get("paths", {}).get("harness_allow", DEFAULT_HARNESS_ALLOW)
    uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
    for pat in patterns:
        pat_exp = os.path.expanduser(pat.replace("{uid}", uid))
        if pat_exp.endswith("/**"):
            if is_within(path, Path(pat_exp[:-3]).resolve()):
                return True
        elif fnmatch.fnmatch(str(path), pat_exp):
            return True
    return False


def matches_readonly_system(path: Path, policy: dict[str, Any]) -> bool:
    for ro in policy.get("paths", {}).get("readonly_system", []):
        if is_within(path, Path(ro).resolve()):
            return True
    return False


def looks_like_path(token: str) -> bool:
    return token.startswith(("/", "~")) or ".." in token.split("/")


# --------------------------------------------------------------------------------------
# Toolchains e coaching
# --------------------------------------------------------------------------------------


def normalize_bin(first_bin: str) -> str:
    """Normaliza binários do workspace (absolutos ou ./) para caminho relativo (.venv/bin/ctx)."""
    p = Path(os.path.expanduser(first_bin))
    if p.is_absolute():
        try:
            return str(p.relative_to(WORKSPACE_ROOT))
        except ValueError:
            return first_bin
    return first_bin[2:] if first_bin.startswith("./") else first_bin


def is_forbidden_toolchain(cmd: str, policy: dict[str, Any]) -> tuple[bool, str]:
    """Valida se o comando invoca diretamente binários proibidos em [toolchains] (Sandbox-Safe)."""
    cmd_stripped = cmd.strip()
    parts = tokenize(cmd_stripped) or cmd_stripped.split()
    if not parts:
        return False, ""

    first_bin = parts[0]
    normalized = normalize_bin(first_bin)
    base_bin = Path(first_bin).name

    for stack_name, tc in policy.get("toolchains", {}).items():
        forbidden = set(tc.get("forbidden_direct", []))
        canonical = tc.get("canonical_runner", "")
        sandbox_runners = set(tc.get("sandbox_runners", []))
        bootstrap_allowed = tc.get("bootstrap_allowed", [])
        reason = tc.get("reason", f"Use o executor canônico '{canonical}'.")

        # Executores locais do .venv do workspace são legítimos (Sandbox-Safe)
        if normalized in sandbox_runners:
            return False, ""

        # Única exceção ao runtime global: criar o .venv quando o uv não existe (ex.: "python3 -m venv")
        if any(cmd_stripped == b or cmd_stripped.startswith(b + " ") for b in bootstrap_allowed):
            return False, ""

        if first_bin in forbidden or normalized in forbidden or base_bin in forbidden:
            if canonical and cmd_stripped.startswith(canonical):
                continue
            alternatives = ", ".join(f"'{r}'" for r in [canonical, *sorted(sandbox_runners)] if r)
            return True, f"Uso direto de '{first_bin}' proibido para stack {stack_name}. {reason} Use: {alternatives}."

    return False, ""


def _has_flag(args: list[str], key_token: str) -> bool:
    if re.fullmatch(r"-[A-Za-z]", key_token):
        letter = key_token[1]
        return any(a.startswith("-") and not a.startswith("--") and letter in a[1:] for a in args)
    return key_token in args


def _git_log_bounded(args: list[str]) -> bool:
    for i, a in enumerate(args):
        if a in ("-n", "--max-count") and i + 1 < len(args):
            return True
        if re.fullmatch(r"-n\d+|-\d+|--max-count=\d+", a):
            return True
    return False


def find_inefficient_variant(segments: list[list[str]], policy: dict[str, Any]) -> str | None:
    """Coaching One-Shot: retorna a mensagem instrutiva se o 1º segmento for um comando ineficiente."""
    variants = policy.get("commands", {}).get("inefficient_variants", {})
    if not variants or not segments or not segments[0]:
        return None

    seg = segments[0]
    bounded_by_pipe = any(s and s[0] in ("head", "tail") for s in segments[1:])

    for key, msg in variants.items():
        if key == "tree_unbounded":
            if seg[0] == "tree" and not bounded_by_pipe and not any(t.startswith("-L") for t in seg[1:]):
                return msg
        elif key == "git_log_unbounded":
            if seg[:2] == ["git", "log"] and not bounded_by_pipe and not _git_log_bounded(seg[2:]):
                return msg
        else:
            key_tokens = key.split()
            if not key_tokens or seg[0] != key_tokens[0]:
                continue
            if all(_has_flag(seg[1:], k) for k in key_tokens[1:]):
                return msg
    return None


# --------------------------------------------------------------------------------------
# Ferramentas nativas de arquivo ([file_tools]): leitura/escrita via shell -> ferramenta do harness
# --------------------------------------------------------------------------------------

READ_BINS = {"cat", "head", "tail", "nl", "less", "more", "bat"}
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?[A-Za-z_]\w*")
DEFAULT_FILE_TOOL_MESSAGES = {
    "claude": {
        "read": (
            "Leitura de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta Read "
            "(file_path + offset/limit para trechos, ex.: offset=120, limit=40); para localizar, ctx search ou rg -n."
        ),
        "write": (
            "Escrita de arquivo via shell bloqueada (file_tools=coach). Use Edit (substituição exata de trecho) "
            "ou Write (arquivo novo ou reescrita completa)."
        ),
    },
    "antigravity": {
        "read": "Leitura de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta nativa de leitura de arquivos do harness.",
        "write": "Escrita de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta nativa de edição/criação de arquivos do harness.",
    },
    "gemini": {
        "read": "Leitura de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta read_file.",
        "write": "Escrita de arquivo via shell bloqueada (file_tools=coach). Use replace (trecho exato) ou write_file (arquivo novo).",
    },
    "cursor": {
        "read": "Leitura de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta de leitura de arquivos do Cursor.",
        "write": "Escrita de arquivo via shell bloqueada (file_tools=coach). Use a ferramenta de edição de arquivos do Cursor.",
    },
    # Codex não tem ferramenta de leitura (lê via shell por design): só a escrita é orientada.
    "codex": {
        "write": "Escrita de arquivo via shell bloqueada (file_tools=coach). Use apply_patch para editar ou criar arquivos.",
    },
}


def _file_args(args: list[str]) -> list[str]:
    """Argumentos posicionais que parecem arquivos (ignora flags, '-' e dispositivos)."""
    return [a for a in args if not a.startswith("-") and not a.startswith("/dev/") and not a.isdigit()]


def classify_file_op(cmd: str) -> str | None:
    """Classifica o comando como leitura ('read') ou escrita ('write') de arquivo via shell, ou None."""
    if HEREDOC_RE.search(cmd):
        return "write"
    tokens = tokenize(HARMLESS_REDIRECT_RE.sub(" ", cmd))
    if not tokens:
        return None
    if any(is_operator(t) and ">" in t for t in tokens):
        return "write"
    segments = split_pipeline(tokens)
    for seg in segments:
        if not seg:
            continue
        head, args = Path(seg[0]).name, seg[1:]
        if head == "tee":
            return "write"
        if head == "sed" and any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
            return "write"
        if head == "perl" and any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args):
            return "write"
    first = segments[0]
    if not first:
        return None
    head, args = Path(first[0]).name, first[1:]
    if head in READ_BINS:
        if head == "tail" and any(a in ("-f", "-F", "--follow") or a.startswith("--follow") for a in args):
            return None
        if _file_args(args):
            return "read"
    if head == "sed" and "-n" in args and len(_file_args(args)) >= 2:
        return "read"
    return None


def file_tools_decision(cmd: str, policy: dict[str, Any], harness: str) -> tuple[str, str] | None:
    """Com [file_tools].mode = "coach", nega leitura/escrita via shell indicando a ferramenta nativa."""
    cfg = policy.get("file_tools", {})
    if cfg.get("mode", "off") != "coach" or harness not in cfg.get("harnesses", list(HARNESSES)):
        return None
    kind = classify_file_op(cmd)
    if kind is None:
        return None
    messages = {**DEFAULT_FILE_TOOL_MESSAGES.get(harness, {}), **cfg.get("messages", {}).get(harness, {})}
    if kind not in messages:
        return None  # ex.: leitura no Codex, que não tem ferramenta de leitura
    return "deny", messages[kind]


def _prefix_match(text: str, prefix: str) -> bool:
    return text == prefix or text.startswith(prefix + " ") or text.startswith(prefix + "\t")


# --------------------------------------------------------------------------------------
# Matriz de decisão: comandos shell
# --------------------------------------------------------------------------------------


def evaluate_shell_command(
    cmd: str, policy: dict[str, Any], cwd: Path | None = None, harness: str = "claude"
) -> tuple[str, str]:
    """Aplica a matriz de decisão de segurança para comandos de terminal."""
    cmd_clean = cmd.strip()
    commands_cfg = policy.get("commands", {})

    if not cmd_clean:
        return "defer", "Comando vazio ou ausente."

    # 1. Padrões destrutivos (deny)
    for pat in commands_cfg.get("deny", []):
        if pat in cmd_clean:
            return "deny", f"Comando bloqueado pela política de segurança (padrão proibido: '{pat}')."

    # 1b. Ferramentas nativas de arquivo (deny instrutivo quando [file_tools].mode = "coach").
    #     Antes do encadeamento para que um heredoc multi-linha receba a orientação certa.
    file_tools = file_tools_decision(cmd_clean, policy, harness)
    if file_tools and not any(dp in cmd_clean for dp in policy.get("paths", {}).get("deny", [])):
        return file_tools

    # 2. Encadeamento de comandos (deny)
    if has_chaining_operators(cmd_clean):
        return (
            "deny",
            "Encadeamento de comandos shell (&&, ||, ;, &, subshells) é proibido: cada comando precisa ser avaliado "
            "sozinho pelo guardrail. Execute um comando por chamada.",
        )

    raw_tokens = tokenize(cmd_clean)
    if raw_tokens is None:
        return "defer", "Aspas não analisáveis; decisão delegada ao modo de permissão do harness."

    # Redirecionamentos inofensivos (2>&1, >/dev/null) não contam como escrita
    tokens = tokenize(HARMLESS_REDIRECT_RE.sub(" ", cmd_clean)) or []
    segments = split_pipeline(tokens)
    if any(not s for s in segments):
        return "defer", "Pipe com segmento vazio; decisão delegada ao harness."

    # 3. Segredos em argumentos (deny)
    for t in tokens:
        if is_operator(t):
            continue
        hit = matches_deny_path(resolve_path(t, cwd), t, policy)
        if hit is None:
            for dp in policy.get("paths", {}).get("deny", []):
                if ("/" in dp or dp.startswith("~")) and (dp in t or os.path.expanduser(dp) in t):
                    hit = dp
                    break
        if hit:
            return "deny", f"Acesso ao caminho sensível restrito '{hit}' é proibido."

    # 4. Toolchains Sandbox-Safe (deny para binários globais)
    for seg in segments:
        is_forbidden, tc_reason = is_forbidden_toolchain(shlex.join(seg), policy)
        if is_forbidden:
            return "deny", tc_reason

    # 5. Coaching One-Shot (deny instrutivo)
    coaching = find_inefficient_variant(segments, policy)
    if coaching:
        return "deny", coaching

    # 6. Ask forte (operações sensíveis)
    if any(is_operator(t) and ">" in t for t in tokens):
        return "ask", "Redirecionamento de saída (>) escreve arquivos e requer confirmação do usuário."
    if "--privileged" in tokens:
        return "ask", "Containers com --privileged requerem confirmação do usuário."

    data_path = btrfs_path(policy)
    data_raw = policy.get("storage", {}).get("btrfs_nocow", {}).get("path", "~/data")
    for needle in {data_raw, str(data_path)}:
        if re.search(re.escape(needle) + r"(?![\w.-])", cmd_clean):
            return "ask", f"Operação envolvendo {data_path} (Btrfs No-COW) requer confirmação do usuário."
    for t in tokens:
        if is_operator(t):
            continue
        candidate = t.split("=", 1)[1] if t.startswith("-") and "=" in t else t
        candidate = candidate.split(":", 1)[0]
        if looks_like_path(candidate) and is_within(resolve_path(candidate, cwd), data_path):
            return "ask", f"Operação envolvendo {data_path} (Btrfs No-COW) requer confirmação do usuário."

    wrappers = set(commands_cfg.get("wrappers", DEFAULT_WRAPPERS))
    for seg in segments:
        head = seg[0]
        if head in PRIVILEGE_ESCALATION:
            return "ask", f"Elevação de privilégio ('{head}') requer confirmação do usuário."
        if head == "tee":
            return "ask", "'tee' escreve arquivos e requer confirmação do usuário."
        if head in wrappers:
            return "ask", f"Wrapper de execução ('{head}') oculta o comando real e requer confirmação do usuário."
        seg_text = " ".join(seg)
        for ask_item in commands_cfg.get("ask", []):
            if _prefix_match(seg_text, ask_item):
                return "ask", f"Comando '{ask_item}' requer confirmação explícita do usuário."

    # 7. Whitelist do 1º comando (fora dela: defer)
    first_seg = segments[0]
    first_text = " ".join([normalize_bin(first_seg[0]), *first_seg[1:]])
    allow_list = commands_cfg.get("allow", [])
    matched_allow = next((a for a in allow_list if _prefix_match(first_text, a)), None)
    if matched_allow is None:
        return "defer", f"Comando '{first_seg[0]}' fora da whitelist; decisão delegada ao modo de permissão do harness."

    # 8. Filtros de pipe seguros (fora deles: defer)
    safe_filters = set(commands_cfg.get("safe_pipe_filters", []))
    for seg in segments[1:]:
        if seg[0] not in safe_filters:
            return "defer", f"Filtro de pipe '{seg[0]}' fora de safe_pipe_filters; decisão delegada ao harness."

    # 9. Leitura de caminhos externos ao workspace (defer)
    for t in tokens:
        if is_operator(t) or not looks_like_path(t):
            continue
        p = resolve_path(t, cwd)
        if is_within(p, WORKSPACE_ROOT) or p == Path("/dev/null"):
            continue
        if matches_harness_allow(p, policy) or matches_readonly_system(p, policy):
            continue
        return "defer", f"Leitura de caminho externo ao workspace ('{t}'); decisão delegada ao harness."

    if len(segments) > 1:
        return "allow", "Pipe simples com filtros seguros aprovado automaticamente."
    return "allow", f"Comando aprovado na whitelist ('{matched_allow}')."


# --------------------------------------------------------------------------------------
# Matriz de decisão: arquivos
# --------------------------------------------------------------------------------------

WRITE_HINTS = ("write", "edit", "create", "delete", "remove", "move", "rename", "replace")


def is_write_tool(tool_name: str) -> bool:
    name = tool_name.lower()
    return any(w in name for w in WRITE_HINTS)


def evaluate_file_access(
    tool_name: str, file_path_str: str, policy: dict[str, Any], cwd: Path | None = None
) -> tuple[str, str]:
    """Aplica a matriz de decisão de segurança para leitura e escrita de arquivos."""
    path = resolve_path(file_path_str, cwd)
    is_write = is_write_tool(tool_name)

    # 1. Caminhos proibidos (deny)
    hit = matches_deny_path(path, file_path_str, policy)
    if hit:
        return "deny", f"Acesso ao arquivo/diretório confidencial '{hit}' é estritamente proibido."

    # 2. Volume Btrfs No-COW (~/data) sempre pergunta
    data_path = btrfs_path(policy)
    if is_within(path, data_path):
        return "ask", f"Operações no volume {data_path} (Btrfs No-COW) requerem confirmação do usuário."

    # 3. Dentro do workspace
    if is_within(path, WORKSPACE_ROOT):
        if is_write:
            for prot in policy.get("paths", {}).get("protected", []):
                if path == (WORKSPACE_ROOT / prot).resolve():
                    return "ask", f"Alteração no arquivo crítico de governança '{prot}' requer confirmação do usuário."
        return "allow", "Operação dentro do escopo do repositório autorizada."

    # 4. Diretórios de trabalho do harness (scratchpad, memória, planos)
    if matches_harness_allow(path, policy):
        return "allow", "Diretório de trabalho do harness autorizado (paths.harness_allow)."

    # 5. Leitura segura de arquivos do sistema
    if not is_write and matches_readonly_system(path, policy):
        return "allow", "Leitura segura em arquivo de sistema (paths.readonly_system)."

    # 6. Fora do workspace
    if is_write:
        return "ask", f"Escrita em '{file_path_str}' fora da raiz do workspace requer confirmação do usuário."
    return "defer", f"Leitura de '{file_path_str}' fora do workspace; decisão delegada ao harness."


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

SHELL_TOOLS = {"bash", "run_command", "terminal", "shell", "run_terminal_command", "run_shell_command"}
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
SEVERITY = {"allow": 0, "defer": 1, "ask": 2, "deny": 3}


def patch_paths(patch: str) -> list[str]:
    """Arquivos tocados por um patch do apply_patch (Codex): Add/Update/Delete File e Move to."""
    return [(m.group(1) or m.group(2)).strip() for m in PATCH_FILE_RE.finditer(patch)]


def evaluate_patch(patch: str, policy: dict[str, Any], cwd: Path | None) -> tuple[str, str]:
    """Avalia cada arquivo do patch como escrita; vale a decisão mais restritiva."""
    paths = patch_paths(patch)
    if not paths:
        return "defer", "Patch sem cabeçalhos de arquivo reconhecíveis; decisão delegada ao harness."
    worst = ("allow", "Patch dentro do escopo do repositório autorizado.")
    for p in paths:
        decision, reason = evaluate_file_access("Write", p, policy, cwd)
        if SEVERITY[decision] > SEVERITY[worst[0]]:
            worst = (decision, reason)
    return worst


def extract_request(payload: dict[str, Any], harness: str) -> tuple[str, str, str]:
    """Normaliza o evento: retorna (tipo, valor, tool_name) com tipo em shell|file|patch|other."""
    event = payload.get("hook_event_name", "")
    if harness == "cursor":
        if event == "beforeShellExecution":
            return "shell", str(payload.get("command") or ""), "Shell"
        if event == "beforeReadFile":
            return "file", str(payload.get("file_path") or ""), "Read"

    tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    tool_args = payload.get("tool_args") or payload.get("tool_input") or payload.get("args") or {}

    if harness == "codex" and tool_name == "apply_patch":
        patch = tool_args.get("command", "") if isinstance(tool_args, dict) else str(tool_args)
        return "patch", str(patch), tool_name

    if isinstance(tool_args, str):
        return "shell", tool_args, tool_name
    if not isinstance(tool_args, dict):
        return "other", "", tool_name

    cmd = tool_args.get("CommandLine") or tool_args.get("command") or tool_args.get("cmd") or ""
    if cmd or tool_name.lower() in SHELL_TOOLS:
        return "shell", str(cmd), tool_name

    file_path = (
        tool_args.get("Path")
        or tool_args.get("path")
        or tool_args.get("file_path")
        or tool_args.get("absolute_path")
        or tool_args.get("notebook_path")
        or ""
    )
    if file_path:
        return "file", str(file_path), tool_name
    return "other", "", tool_name


def main() -> None:
    """Entry point do hook (PreToolUse / BeforeTool / beforeShellExecution / beforeReadFile)."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.stdout.write(json.dumps({"decision": "allow", "reason": "Entrada vazia"}))
            return
        payload = json.loads(raw_input)
    except Exception as e:
        sys.stdout.write(json.dumps({"decision": "allow", "reason": f"Fallback JSON: {e}"}))
        return

    harness = detect_harness(payload)
    policy = load_policy()
    harness_cfg = policy.get("harnesses", {}).get(harness, {})
    ask_fallback = harness_cfg.get("ask_fallback", "deny") if isinstance(harness_cfg, dict) else "deny"
    cwd_raw = payload.get("cwd") or (payload.get("workspace_roots") or [None])[0]
    cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None

    kind, value, tool_name = extract_request(payload, harness)
    if kind == "shell":
        decision, reason = evaluate_shell_command(value, policy, cwd, harness=harness)
    elif kind == "patch":
        decision, reason = evaluate_patch(value, policy, cwd)
    elif kind == "file":
        decision, reason = evaluate_file_access(tool_name, value, policy, cwd)
    else:
        decision, reason = "allow", "Ferramenta padrão autorizada."

    sys.stdout.write(json.dumps(format_response(harness, decision, reason, ask_fallback=ask_fallback)))


if __name__ == "__main__":
    main()
