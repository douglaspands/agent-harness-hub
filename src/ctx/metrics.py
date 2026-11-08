"""Métricas de uso de ferramentas de arquivo a partir dos transcripts do Claude Code (`ctx metrics`).

Mede, por sessão, leituras/escritas feitas pelas ferramentas nativas (Read/Edit/Write) versus via shell,
e quantos `deny` de coaching ([file_tools].mode = "coach") ocorreram. Usa o mesmo classificador do
guardrail (kit/guardrail.py), então "via shell" aqui é exatamente o que o modo coach bloquearia.

100% biblioteca padrão.
"""

from __future__ import annotations

import importlib.util
import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

NATIVE_READ = {"Read"}
NATIVE_WRITE = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash"}
COACH_MARKER = "file_tools=coach"
COUNTERS = ("native_read", "bash_read", "native_write", "bash_write", "coach_denies")


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


def analyze_session(path: Path) -> dict[str, Any]:
    classify = guardrail_module().classify_file_op
    stats: dict[str, Any] = {k: 0 for k in COUNTERS}
    stats.update({"session": path.stem, "started": ""})
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not stats["started"] and isinstance(entry.get("timestamp"), str):
            stats["started"] = entry["timestamp"][:16].replace("T", " ")
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name")
                if name in NATIVE_READ:
                    stats["native_read"] += 1
                elif name in NATIVE_WRITE:
                    stats["native_write"] += 1
                elif name in SHELL_TOOLS:
                    kind = classify(str((block.get("input") or {}).get("command", "")))
                    if kind:
                        stats[f"bash_{kind}"] += 1
            elif block.get("type") == "tool_result" and COACH_MARKER in _result_text(block):
                stats["coach_denies"] += 1
    return stats


def _pct(part: int, total: int) -> float | None:
    return round(100 * part / total, 1) if total else None


def analyze(transcripts_dir: Path) -> dict[str, Any]:
    sessions = [analyze_session(p) for p in sorted(transcripts_dir.rglob("*.jsonl"))]
    sessions = [s for s in sessions if any(s[k] for k in COUNTERS)]
    sessions.sort(key=lambda s: s["started"])
    totals = {k: sum(s[k] for s in sessions) for k in COUNTERS}
    totals["bash_read_pct"] = _pct(totals["bash_read"], totals["bash_read"] + totals["native_read"])
    totals["bash_write_pct"] = _pct(totals["bash_write"], totals["bash_write"] + totals["native_write"])
    return {"transcripts_dir": str(transcripts_dir), "sessions": sessions, "totals": totals}
