"""Métricas de uso do ctx a partir dos transcripts do Claude Code e do log de eventos local (`ctx metrics`).

Por sessão mede:
- ferramentas de arquivo: leituras/escritas nativas (Read/Edit/Write) vs via shell, e `deny` de coaching
  ([file_tools].mode = "coach"), com o mesmo classificador do guardrail (kit/guardrail.py);
- uso do ctx: buscas (`ctx search`, e quantas queries), mapa/status, recall, fatos, upstream e receitas, contra os
  desvios para `rg`/`grep`/`sed -n`;
- desperdício: comandos repetidos, re-downloads da mesma URL e bloqueios do guardrail;
- tamanho: chamadas de ferramenta e caracteres de resultado (base das estimativas de tokens), modelo usado;
- do `.ctx/events.jsonl`: latência por comando do ctx, chamadas economizadas por receita, acertos do recall,
  hits do cache upstream e dicas de receita emitidas pelo guardrail.

100% biblioteca padrão.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import statistics
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

from ctx.events import read_events

NATIVE_READ = {"Read"}
NATIVE_WRITE = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash"}
FETCH_TOOLS = {"WebFetch"}
COACH_MARKER = "file_tools=coach"
GUARDRAIL_DENY_RE = re.compile(r"PreToolUse:\w+ hook error|hook error:")
RULE_TAG_RE = re.compile(r"\[regra: ([\w:.-]+)\]")
COUNTERS = ("native_read", "bash_read", "native_write", "bash_write", "coach_denies")
CTX_COUNTERS = (
    "ctx_search", "ctx_queries", "ctx_map", "ctx_status", "ctx_recall", "ctx_fact", "ctx_upstream", "ctx_recipe",
    "rg_detours",
)
WASTE_COUNTERS = ("repeated_cmds", "redownloads", "denials")
SIZE_COUNTERS = ("calls", "result_chars")
ALL_NUMERIC = COUNTERS + CTX_COUNTERS + WASTE_COUNTERS + SIZE_COUNTERS
SEARCH_VALUE_FLAGS = {"--top", "--path", "--lines", "--timeout", "--scope"}
DETOUR_PROGRAMS = {"rg", "grep", "egrep", "fgrep", "ag"}
TRIVIAL_REPEATS = {"ls", "pwd", "git status", "git diff", "git log", "date", "clear", "true"}
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
SESSION_SLACK_S = 120


@lru_cache(maxsize=1)
def guardrail_module() -> ModuleType:
    with resources.as_file(resources.files("ctx") / "kit" / "guardrail.py") as path:
        spec = importlib.util.spec_from_file_location("ctx_kit_guardrail", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def transcripts_dir_for(workspace_root: Path) -> Path:
    """Diretório onde o Claude Code guarda os transcripts do projeto (~/.claude/projects/<caminho-normalizado>)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace_root.resolve()))


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _argv(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def ctx_subcommand(cmd: str) -> tuple[str, list[str]] | None:
    """('search', args) se o comando é uma chamada do ctx (`ctx`, `.venv/bin/ctx`, `uv run ctx`), senão None."""
    tokens = _argv(cmd)
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if not tokens or os.path.basename(tokens[0]) != "ctx":
        return None
    rest = tokens[1:]
    sub = next((t for t in rest if not t.startswith("-")), None)
    if sub is None:
        return None
    return sub, rest[rest.index(sub) + 1 :]


def count_queries(args: list[str]) -> int:
    """Quantas queries um `ctx search` recebeu (posicionais, sem flags nem valores de flags)."""
    n, skip = 0, False
    for a in args:
        if skip:
            skip = False
        elif a in SEARCH_VALUE_FLAGS:
            skip = True
        elif a == "|":
            break
        elif not a.startswith("-"):
            n += 1
    return n


def is_detour(cmd: str) -> bool:
    tokens = _argv(cmd)
    if not tokens:
        return False
    head = os.path.basename(tokens[0])
    return head in DETOUR_PROGRAMS or (head == "sed" and "-n" in tokens[1:])


# Mensagens do guardrail anterior à 0.5 (sem `[regra: …]`) -> id atual, para antigos e novos somarem na mesma linha
LEGACY_RULES = (
    ("Encadeamento de comandos", "chaining"),
    ("padrão proibido: 'mkfs", "destructive:mkfs"),
    ("padrão proibido: 'rm -", "destructive:rm"),
    ("padrão proibido: 'curl", "destructive:pipe-shell"),
    ("padrão proibido: 'wget", "destructive:pipe-shell"),
    ("Sempre limite o git log", "coach:git_log_unbounded"),
    ("Sempre passe -L", "coach:tree_unbounded"),
    ("Leitura de arquivo via shell bloqueada", "file-tools:read"),
    ("Escrita de arquivo via shell bloqueada", "file-tools:write"),
    ("caminho sensível restrito", "secret-path"),
    ("arquivo/diretório confidencial", "secret-path"),
    ("proibido para stack", "toolchain"),
)


def denial_rule(text: str) -> str:
    """Id da regra de um bloqueio (`[regra: <id>]`, guardrail 0.5+) ou, em transcripts antigos, o id equivalente da
    mensagem conhecida; sem correspondência, o início da mensagem."""
    m = RULE_TAG_RE.search(text)
    if m:
        return m.group(1)
    for needle, rule in LEGACY_RULES:
        if needle in text:
            return rule
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    first = first.strip().lstrip(":").strip().strip("\"')]}").strip()
    return re.sub(r"\d+", "N", first)[:70] if re.search(r"\w{3}", first) else "(sem texto)"


def new_stats(path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {k: 0 for k in ALL_NUMERIC}
    stats.update({
        "session": path.stem, "started": "", "model": "", "start_ts": 0.0, "end_ts": 0.0,
        "denial_rules": {}, "denial_repeats": 0,
    })
    return stats


def _iso_ts(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def analyze_session(path: Path) -> dict[str, Any]:
    classify = guardrail_module().classify_file_op
    stats = new_stats(path)
    commands: Counter[str] = Counter()
    urls: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("timestamp")
        if isinstance(ts_raw, str):
            if not stats["started"]:
                stats["started"] = ts_raw[:16].replace("T", " ")
            ts = _iso_ts(ts_raw)
            if ts:
                stats["start_ts"] = stats["start_ts"] or ts
                stats["end_ts"] = max(stats["end_ts"], ts)
        message = entry.get("message") or {}
        if not stats["model"] and isinstance(message.get("model"), str):
            stats["model"] = message["model"]
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                _count_tool_use(stats, block, classify, commands, urls)
            elif block.get("type") == "tool_result":
                text = _result_text(block)
                stats["result_chars"] += len(text)
                if COACH_MARKER in text:
                    stats["coach_denies"] += 1
                m = GUARDRAIL_DENY_RE.search(text)
                if m:
                    stats["denials"] += 1
                    # por regra; a mesma regra de novo na sessão = coaching que não ensinou (M-19)
                    rule = denial_rule(text[m.end() :])
                    stats["denial_rules"][rule] = stats["denial_rules"].get(rule, 0) + 1
                    if stats["denial_rules"][rule] > 1:
                        stats["denial_repeats"] += 1
    stats["repeated_cmds"] = sum(n - 1 for c, n in commands.items() if n > 1 and c not in TRIVIAL_REPEATS)
    stats["redownloads"] = sum(n - 1 for n in urls.values() if n > 1)
    return stats


def _count_tool_use(stats: dict[str, Any], block: dict[str, Any], classify: Any, commands: Counter[str], urls: Counter[str]) -> None:
    name = block.get("name")
    inp = block.get("input") or {}
    stats["calls"] += 1
    if name in NATIVE_READ:
        stats["native_read"] += 1
    elif name in NATIVE_WRITE:
        stats["native_write"] += 1
    elif name in FETCH_TOOLS and inp.get("url"):
        urls[str(inp["url"])] += 1
    elif name in SHELL_TOOLS:
        cmd = str(inp.get("command", ""))
        kind = classify(cmd)
        if kind:
            stats[f"bash_{kind}"] += 1
        commands[cmd.strip()] += 1
        for url in URL_RE.findall(cmd) if _argv(cmd)[:1] and os.path.basename(_argv(cmd)[0]) in ("curl", "wget") else []:
            urls[url] += 1
        ctx = ctx_subcommand(cmd)
        if ctx:
            sub, args = ctx
            if sub == "search":
                stats["ctx_search"] += 1
                stats["ctx_queries"] += count_queries(args)
            elif sub in ("map", "status", "recall", "fact", "upstream"):
                stats[f"ctx_{sub}"] += 1
            elif sub == "recipe" and args[:1] == ["run"]:
                stats["ctx_recipe"] += 1
        elif is_detour(cmd):
            stats["rg_detours"] += 1


def _pct(part: int, total: int) -> float | None:
    return round(100 * part / total, 1) if total else None


def assign_events(sessions: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distribui os eventos do ctx pelas sessões cuja janela de tempo os contém; devolve os sem sessão."""
    for s in sessions:
        s["events"] = []
    orphans: list[dict[str, Any]] = []
    for ev in events:
        ts = float(ev.get("ts", 0))
        owner = None
        for s in sessions:
            if s["start_ts"] - SESSION_SLACK_S <= ts <= s["end_ts"] + SESSION_SLACK_S:
                owner = s if owner is None or s["start_ts"] > owner["start_ts"] else owner
        (owner["events"] if owner else orphans).append(ev)
    return orphans


def event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Latência por comando, chamadas economizadas por receita, acertos do recall/fatos e cache do upstream."""
    by_cmd: dict[str, list[float]] = {}
    out: dict[str, Any] = {
        "recipe_runs": 0, "recipe_ok": 0, "recipe_steps_saved": 0, "recall_queries": 0, "recall_hits": 0,
        "fact_searches": 0, "fact_hits": 0, "upstream_gets": 0, "upstream_cached": 0, "hints": 0,
    }
    for ev in events:
        cmd = str(ev.get("cmd", ""))
        if "latency_ms" in ev:
            by_cmd.setdefault(cmd, []).append(float(ev["latency_ms"]))
        if cmd == "recipe run":
            out["recipe_runs"] += 1
            out["recipe_ok"] += 1 if ev.get("status") == "ok" else 0
            out["recipe_steps_saved"] += int(ev.get("steps_saved", 0))
        elif cmd == "recall":
            out["recall_queries"] += 1
            out["recall_hits"] += 1 if ev.get("hit") else 0
        elif cmd == "fact search":
            out["fact_searches"] += 1
            out["fact_hits"] += 1 if ev.get("hit") else 0
        elif cmd == "upstream get":
            out["upstream_gets"] += 1
            out["upstream_cached"] += 1 if ev.get("cached") else 0
        elif cmd == "hint":
            out["hints"] += 1
        elif cmd == "guardrail" and ev.get("decision") == "deny":
            rule = str(ev.get("rule") or "?")
            out.setdefault("guardrail_rules", {})[rule] = out.get("guardrail_rules", {}).get(rule, 0) + 1
    latency = {}
    for cmd, vals in sorted(by_cmd.items()):
        vals.sort()
        latency[cmd] = {
            "n": len(vals),
            "p50_ms": round(statistics.median(vals), 1),
            "p95_ms": round(vals[min(len(vals) - 1, int(0.95 * len(vals)))], 1),
            "max_ms": round(vals[-1], 1),
        }
    out["latency"] = latency
    return out


def analyze(transcripts_dir: Path, workspace_root: Path | None = None, since_ts: float = 0.0) -> dict[str, Any]:
    """Relatório por sessão + totais. Com `workspace_root`, inclui o resumo do log de eventos do ctx."""
    sessions = [analyze_session(p) for p in sorted(transcripts_dir.rglob("*.jsonl"))]
    keys = COUNTERS + CTX_COUNTERS
    sessions = [s for s in sessions if any(s[k] for k in keys) and s["end_ts"] >= since_ts]
    sessions.sort(key=lambda s: s["started"])
    totals = {k: sum(s[k] for s in sessions) for k in ALL_NUMERIC}
    totals["bash_read_pct"] = _pct(totals["bash_read"], totals["bash_read"] + totals["native_read"])
    totals["bash_write_pct"] = _pct(totals["bash_write"], totals["bash_write"] + totals["native_write"])
    totals["ctx_search_pct"] = _pct(totals["ctx_search"], totals["ctx_search"] + totals["rg_detours"])
    result: dict[str, Any] = {"transcripts_dir": str(transcripts_dir), "sessions": sessions, "totals": totals}
    if workspace_root is not None:
        events = read_events(workspace_root, since_ts)
        orphans = assign_events(sessions, events)
        for s in sessions:
            s["ctx_events"] = event_summary(s["events"])
        result["events"] = event_summary(events)
        result["orphan_events"] = len(orphans)
    return result
