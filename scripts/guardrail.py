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
import time
import tomllib
from pathlib import Path
from typing import Any

# Versão do motor `ctx` que gerou este arquivo: `ctx doctor` compara com a versão instalada e pede `ctx upgrade`.
KIT_VERSION = "0.4.0"

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
# Padrões destrutivos casados com o argv (M-24) e scripts citados para outro shell (M-25)
# --------------------------------------------------------------------------------------

# Entradas conhecidas de commands.deny -> regra estrutural (o texto continua sendo o rótulo da mensagem).
# As regras estruturais valem sempre; entradas desconhecidas casam como prefixo do argv de cada comando.
KNOWN_DENY = {
    "rm -rf /": "rm",
    "mkfs": "mkfs",
    ":(){ :|:& };:": "forkbomb",
    "curl | bash": "pipe-shell",
    "wget | bash": "pipe-shell",
    "curl | sh": "pipe-shell",
    "wget | sh": "pipe-shell",
}
RULE_LABELS = {"rm": "rm -rf /", "mkfs": "mkfs", "forkbomb": ":(){ :|:& };:", "pipe-shell": "curl | bash"}
SYSTEM_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root", "/sbin",
    "/srv", "/sys", "/usr", "/var",
}
HOME_TARGETS = {"~", "$HOME", "${HOME}"}
SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ash", "ksh"}
# Prefixos que só repassam o comando (o programa real vem depois deles e das opções deles)
PASSTHROUGH = {"sudo", "doas", "run0", "pkexec", "exec", "env", "nice", "nohup", "command", "time", "timeout", "stdbuf", "ionice"}
_OPT_WITH_VALUE = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "env": {"-u", "-C", "-S"},
}
_SSH_OPT_WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=")
SEPARATORS = CHAIN_TOKENS | {"|"}


def strip_passthrough(argv: list[str]) -> list[str]:
    """Tira `VAR=x`, `sudo -u u`, `env`, `timeout 5`… do início: o que sobra começa no programa real."""
    i = 0
    while i < len(argv):
        word = argv[i].lstrip("`")
        name = Path(word).name
        if _ASSIGNMENT_RE.match(word):
            i += 1
            continue
        if name not in PASSTHROUGH:
            break
        i += 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
            opt = argv[i]
            i += 1
            if opt in _OPT_WITH_VALUE.get(name, ()) and i < len(argv):
                i += 1
        if i < len(argv) and argv[i] == "--":
            i += 1
        if name == "timeout" and i < len(argv) and re.fullmatch(r"[\d.]+[smhd]?", argv[i]):
            i += 1
    out = argv[i:]
    return [out[0].lstrip("`"), *out[1:]] if out else out


def pipelines(tokens: list[str]) -> list[list[list[str]]]:
    """Tokens -> pipelines (separadas por ; && || & ( )) -> comandos simples (separados por |), sem operadores."""
    out: list[list[list[str]]] = [[[]]]
    for t in tokens:
        if t in SEPARATORS or (is_operator(t) and any(c in t for c in ";()") and ">" not in t and "<" not in t):
            if t == "|" or t == "|&":
                out[-1].append([])
            else:
                out.append([[]])
        elif is_operator(t):
            continue  # redirecionamento: o alvo fica como argumento, inofensivo para as regras abaixo
        else:
            out[-1][-1].append(t.rstrip("`") if t.endswith("`") and not t.startswith("`") else t)
    return [[strip_passthrough(c) for c in p if c] for p in out if any(p)]


def _rm_target_is_dangerous(arg: str) -> bool:
    a = arg[:-2] if arg.endswith("/*") else arg
    a = a.rstrip("/") or "/"
    return a in SYSTEM_DIRS or a in HOME_TARGETS or a == os.path.expanduser("~").rstrip("/")


def _rm_is_destructive(args: list[str]) -> bool:
    recursive = False
    targets: list[str] = []
    options = True
    for a in args:
        if options and a == "--":
            options = False
        elif options and a.startswith("--"):
            if a == "--no-preserve-root":
                return True
            recursive = recursive or a == "--recursive"
        elif options and a.startswith("-") and len(a) > 1:
            recursive = recursive or "r" in a[1:] or "R" in a[1:]
        else:
            targets.append(a)
    return recursive and any(_rm_target_is_dangerous(t) for t in targets)


def embedded_scripts(argv: list[str]) -> list[str]:
    """Scripts que o comando entrega a outro shell: `sh -c '<s>'` (também dentro de podman/docker/sudo) e
    `ssh [opções] host <s...>`."""
    out: list[str] = []
    if not argv:
        return out
    if Path(argv[0]).name == "ssh":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            opt = argv[i]
            i += 1
            if len(opt) == 2 and opt[1] in _SSH_OPT_WITH_VALUE and i < len(argv):
                i += 1
        if i + 1 < len(argv):
            out.append(" ".join(argv[i + 1 :]))
        return out
    for i, word in enumerate(argv[:-1]):
        if Path(word).name not in SHELL_NAMES:
            continue
        for j in range(i + 1, len(argv) - 1):
            opt = argv[j]
            if not opt.startswith("-") or opt.startswith("--"):
                break
            if "c" in opt[1:]:
                out.append(argv[j + 1])
                break
    return out


def destructive_rule(cmd: str, policy: dict[str, Any], depth: int = 0) -> tuple[str, str] | None:
    """(regra, rótulo do padrão) se o comando (ou um script embutido nele) é destrutivo; senão None."""
    deny_entries = [str(p) for p in policy.get("commands", {}).get("deny", [])]
    if ":(){" in cmd.replace(" ", ""):
        return "forkbomb", RULE_LABELS["forkbomb"]
    tokens = tokenize(HARMLESS_REDIRECT_RE.sub(" ", cmd))
    if tokens is None:
        # Aspas que não fecham: conservador, substring como antes
        for pat in deny_entries:
            if pat in cmd:
                return KNOWN_DENY.get(pat, "custom"), pat
        return None
    custom = [p for p in deny_entries if p not in KNOWN_DENY]
    for pipe in pipelines(tokens):
        names = [Path(c[0]).name for c in pipe if c]
        for k, name in enumerate(names):
            if name in ("curl", "wget") and any(n in SHELL_NAMES for n in names[k + 1 :]):
                return "pipe-shell", RULE_LABELS["pipe-shell"]
        for argv in pipe:
            if not argv:
                continue
            name = Path(argv[0]).name
            if name == "rm" and _rm_is_destructive(argv[1:]):
                return "rm", RULE_LABELS["rm"]
            if name.startswith("mkfs"):
                return "mkfs", RULE_LABELS["mkfs"]
            text = " ".join(argv)
            for pat in custom:
                if _prefix_match(text, pat) or (is_operator_text(pat) and pat in cmd):
                    return "custom", pat
            if depth < 4:
                for script in embedded_scripts(argv):
                    hit = destructive_rule(script, policy, depth + 1)
                    if hit:
                        return hit
    return None


def is_operator_text(pattern: str) -> bool:
    """Padrão customizado com operadores de shell (ex.: 'a | b'): casa como substring, como antes."""
    return any(ch in PUNCTUATION for ch in pattern)


def _quoted_spans(cmd: str) -> list[tuple[int, int, str]]:
    """(início, fim exclusivo, aspa) de cada trecho entre aspas simples ou duplas, fora de outras aspas."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and cmd[j] != ch:
                j += 2 if (ch == '"' and cmd[j] == "\\") else 1
            if j >= n:
                return spans  # aspas sem fechar: o tokenizador decide
            spans.append((i, j + 1, ch))
            i = j + 1
            continue
        i += 1
    return spans


_SHELL_C_PREFIX_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:" + "|".join(sorted(SHELL_NAMES)) + r")\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*c[A-Za-z]*\s+$"
)
_SSH_PREFIX_RE = re.compile(r"^\s*(?:(?:sudo|doas|exec|command|nice|nohup)\s+)*(?:\S*/)?ssh\s+(?:\S+\s+)*\S+\s+$")


def mask_remote_scripts(cmd: str) -> tuple[str, bool]:
    """Troca por um marcador o texto citado que vai para outro shell (`sh -c '…'`, `ssh host '…'`,
    `podman run … sh -c '…'`): o encadeamento DENTRO dele não é local. Em aspas duplas, `$(…)` e crase
    rodam na máquina local, então o trecho fica (e o encadeamento é recusado). Retorna (texto, houve_subst_local)."""
    out: list[str] = []
    pos = 0
    local_subst = False
    for start, end, quote in _quoted_spans(cmd):
        prefix = "".join(out) + cmd[pos:start]
        body = cmd[start + 1 : end - 1]
        carrier = bool(_SHELL_C_PREFIX_RE.search(prefix) or _SSH_PREFIX_RE.match(prefix))
        if carrier and quote == '"' and ("$(" in body or "`" in body):
            local_subst = True
            carrier = False
        out.append(cmd[pos:start])
        out.append("'__script__'" if carrier else cmd[start:end])
        pos = end
    out.append(cmd[pos:])
    return "".join(out), local_subst


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


def is_recipe_override(segments: list[list[str]]) -> bool:
    """`ctx recipe run <nome> --yes`: o agente não pode aprovar sozinho etapas que o guardrail não liberou."""
    for seg in segments:
        words = [Path(seg[0]).name, *seg[1:]] if seg else []
        if "recipe" in words and "run" in words and any(w in ("--yes", "-y") for w in words):
            return True
    return False


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

    # 1. Padrões destrutivos (deny): casados com o argv (programa e alvos), também dentro de `sh -c`/`ssh`
    hit = destructive_rule(cmd_clean, policy)
    if hit:
        return "deny", f"Comando bloqueado pela política de segurança (padrão proibido: '{hit[1]}')."

    # 1b. Ferramentas nativas de arquivo (deny instrutivo quando [file_tools].mode = "coach").
    #     Antes do encadeamento para que um heredoc multi-linha receba a orientação certa.
    file_tools = file_tools_decision(cmd_clean, policy, harness)
    if file_tools and not any(dp in cmd_clean for dp in policy.get("paths", {}).get("deny", [])):
        return file_tools

    # 2. Encadeamento de comandos (deny). O script citado de `ssh host '…'`/`sh -c '…'` roda em outro shell:
    #    só as regras destrutivas (passo 1) valem para ele.
    masked, local_subst = mask_remote_scripts(cmd_clean)
    if has_chaining_operators(masked):
        if local_subst:
            return (
                "deny",
                "Em aspas duplas, `$(…)` e crase rodam na máquina LOCAL antes do ssh/sh -c. Passe o script em aspas "
                "simples (ex.: ssh host 'x=$(cmd) ; echo $x'), que ele vai inteiro para o outro shell.",
            )
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
    if is_recipe_override(segments):
        return (
            "ask",
            "`ctx recipe run ... --yes` executa etapas que o guardrail não aprovou sozinho (ask/defer) e requer "
            "confirmação do usuário.",
        )
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


# --------------------------------------------------------------------------------------
# Sugestão de receita: o agente repetiu à mão a sequência de uma receita (`ctx recipe run`)
# --------------------------------------------------------------------------------------

HINT_WINDOW_S = 900
HINT_KEEP = 8
# Receitas embutidas no ctx (o guardrail não importa o ctx): nome -> (params, etapas com `*` no lugar dos parâmetros)
BUILTIN_RECIPE_HINTS = {"copy-verify": (["origem", "destino"], ["cp * *", "sync", "sha256sum *", "sha256sum *"])}


def recipe_patterns(policy: dict[str, Any]) -> dict[str, tuple[list[str], list[str]]]:
    """Receitas com 2+ etapas de comando: nome -> (params, padrões de etapa). Policy sobrescreve as embutidas."""
    out = dict(BUILTIN_RECIPE_HINTS)
    recipes = policy.get("recipes", {})
    for name, r in (recipes.items() if isinstance(recipes, dict) else []):
        if not isinstance(r, dict):
            continue
        steps = []
        for s in r.get("steps", []):
            cmd = s if isinstance(s, str) else (s.get("run") if isinstance(s, dict) else None)
            if cmd:
                steps.append(re.sub(r"\{\w+\}", "*", cmd))
        out[name] = (list(r.get("params", [])), steps)
    return out


def _step_matches(cmd: str, pattern: str) -> bool:
    try:
        ct, pt = shlex.split(cmd), shlex.split(pattern)
    except ValueError:
        return False
    return len(ct) == len(pt) and all(fnmatch.fnmatchcase(c, p) for c, p in zip(ct, pt))


def suggest_recipe(history: list[str], policy: dict[str, Any]) -> str | None:
    """Se os 2 últimos comandos são etapas consecutivas de uma receita, devolve a dica `ctx recipe run ...`."""
    if len(history) < 2:
        return None
    runner = policy.get("ctx", {}).get("runner", ".venv/bin/ctx")
    for name, (params, steps) in recipe_patterns(policy).items():
        for i in range(len(steps) - 1):
            if _step_matches(history[-2], steps[i]) and _step_matches(history[-1], steps[i + 1]):
                usage = " ".join(f"<{p}>" for p in params)
                return (
                    f"Dica: esta sequência manual é a receita `{name}`. Rode `{runner} recipe run {name} {usage}`".rstrip()
                    + " — uma chamada em vez de uma por etapa (cada etapa passa pelo guardrail)."
                )
    return None


def record_and_suggest(cmd: str, policy: dict[str, Any]) -> str | None:
    """Guarda o comando num histórico curto (.ctx/hint-history.json) e sugere a receita. Best-effort: nunca falha."""
    try:
        if "recipe" in cmd.split() and "run" in cmd.split():
            return None
        path = WORKSPACE_ROOT / ".ctx" / "hint-history.json"
        now = time.time()
        try:
            hist = [h for h in json.loads(path.read_text(encoding="utf-8")) if now - h["ts"] < HINT_WINDOW_S]
        except (OSError, ValueError, KeyError, TypeError):
            hist = []
        hist.append({"ts": now, "cmd": cmd.strip()})
        hist = hist[-HINT_KEEP:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hist), encoding="utf-8")
        hint = suggest_recipe([h["cmd"] for h in hist], policy)
        if hint:
            with open(WORKSPACE_ROOT / ".ctx" / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(now, 3), "cmd": "hint", "recipe": hint.split("`")[1]}) + "\n")
        return hint
    except Exception:  # noqa: BLE001 - dica é opcional
        return None


def with_hint(response: dict[str, Any], decision: str, hint: str | None, harness: str) -> dict[str, Any]:
    """Anexa a dica sem alterar a decisão: contexto adicional (Claude/Codex) ou no motivo de ask/deny."""
    if not hint:
        return response
    if decision in ("ask", "deny"):
        for key in ("permissionDecisionReason", "reason", "agent_message", "user_message"):
            holder = response.get("hookSpecificOutput", response)
            if key in holder:
                holder[key] = f"{holder[key]} {hint}"
        return response
    if harness in ("claude", "codex"):
        out = response.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
        out["additionalContext"] = hint
    return response


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

    response = format_response(harness, decision, reason, ask_fallback=ask_fallback)
    if kind == "shell" and decision != "deny":
        response = with_hint(response, decision, record_and_suggest(value, policy), harness)
    sys.stdout.write(json.dumps(response))


if __name__ == "__main__":
    main()
