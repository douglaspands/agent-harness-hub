"""Captura de fim de sessão: transforma o que a sessão aprendeu em CANDIDATOS a fato (nunca em fatos).

Extrai do transcript indexado (recall):
- erros e o que os resolveu: uma chamada que falhou seguida, logo depois, de uma chamada bem-sucedida da mesma
  família de comando (mesmo programa e subcomando);
- comandos bem-sucedidos de programas "notáveis" (`[recall].notable_programs`: build, testes, deploy...);
- URLs usadas com sucesso (WebFetch, curl, wget, git clone...), para não baixar/descobrir de novo.

Os candidatos ficam em `fact_candidates` até a revisão humana (`ctx fact review`). Roda como hook de parada do
harness (`ctx recall capture --from-hook`, opt-in via `[recall].capture_on_stop`): silencioso, rápido e nunca falha.
Tudo local; `paths.deny` e a redação de segredos valem para o que é lido do índice.

100% biblioteca padrão.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from ctx.config import load_config, load_raw_policy
from ctx.facts import add_candidates
from ctx.memory_db import open_memory
from ctx.recall import URL_RE, Privacy, sync_recall, transcript_files

TRIVIAL = {"ls", "cd", "pwd", "cat", "echo", "head", "tail", "wc", "true", "false", "which", "sleep", "clear", "tree", "rg", "grep", "fd", "find"}
LOOKAHEAD = 15
FETCH_TOOLS = {"WebFetch", "web_fetch", "fetch"}
URL_PROGRAMS = {"curl", "wget", "gh", "git", "aria2c", "http"}


def _argv(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def family(cmd: str) -> tuple[str, str]:
    """(programa, subcomando) para casar uma tentativa que falhou com a que funcionou."""
    argv = _argv(cmd)
    if not argv:
        return "", ""
    prog = os.path.basename(argv[0])
    sub = next((a for a in argv[1:2] if not a.startswith("-")), "")
    return prog, sub


def short(cmd: str, n: int = 160) -> str:
    cmd = re.sub(r"\s+", " ", cmd.strip())
    return cmd if len(cmd) <= n else cmd[: n - 1] + "…"


def first_error_line(result: str) -> str:
    for line in result.splitlines():
        low = line.lower()
        if any(w in low for w in ("error", "erro", "fail", "not found", "no such", "denied", "cannot", "unable", "fatal")):
            return short(line.strip(), 140)
    lines = [ln for ln in result.splitlines() if ln.strip() and not ln.lower().startswith("exit code")]
    return short(lines[0], 140) if lines else "sem mensagem"


def extract_candidates(calls: list[dict[str, Any]], config: dict[str, Any], privacy: Privacy, runner: str) -> list[dict[str, Any]]:
    """Candidatos de uma sessão (chamadas em ordem cronológica), do mais valioso ao menos, limitados a `max_candidates`."""
    notable = set(config["recall"]["notable_programs"])
    limit = int(config["recall"]["max_candidates"])
    runner_prog = os.path.basename(runner.split()[0]) if runner else "ctx"
    resolved: list[dict[str, Any]] = []
    commands: dict[str, dict[str, Any]] = {}
    urls: dict[str, dict[str, Any]] = {}

    bash = [c for c in calls if c["tool"] == "Bash" and c["command"] and not privacy.blocked(c["command"])]
    for i, c in enumerate(bash):
        prog, sub = family(c["command"])
        if not prog or prog in TRIVIAL or prog == runner_prog:
            continue
        if c["ok"] == 0:
            window = [b for b in bash[i + 1 : i + 1 + LOOKAHEAD] if b["ok"] == 1 and b["command"] != c["command"]]
            # prefere a mesma família (programa + subcomando); senão o mesmo programa (ex.: `make firmware` -> `make all`)
            same = [b for b in window if family(b["command"]) == (prog, sub)] or [b for b in window if family(b["command"])[0] == prog]
            if same:
                later = same[0]
                resolved.append(
                    {
                        "text": f"Erro em `{short(c['command'])}`: {first_error_line(c['result'])}. Resolvido com `{short(later['command'])}`.",
                        "source": later["src"], "tags": ["erro", "resolvido", prog], "session": c["session"],
                    }
                )
        elif c["ok"] == 1 and prog in notable:
            commands[c["command"]] = {
                "text": f"Comando que funcionou: `{short(c['command'])}`" + (f" ({short(c['text'], 80)})" if c["text"] else ""),
                "source": c["src"], "tags": ["comando", "funcionou", prog], "session": c["session"],
            }
    for c in calls:
        if c["ok"] != 1 or privacy.blocked(c["command"]):
            continue
        found: list[str] = []
        if c["tool"] in FETCH_TOOLS:
            found = URL_RE.findall(c["command"])
        elif c["tool"] == "Bash" and family(c["command"])[0] in URL_PROGRAMS:
            found = URL_RE.findall(c["command"])
        for url in found:
            urls[url.rstrip(".,;")] = {
                "text": f"URL usada com sucesso: {url.rstrip('.,;')}" + (f" ({short(c['text'], 80)})" if c["text"] else ""),
                "source": c["src"], "tags": ["url"], "session": c["session"],
            }
    # mais recentes primeiro dentro de cada grupo; erros resolvidos > URLs > comandos
    ordered = list(reversed(resolved)) + list(reversed(list(urls.values()))) + list(reversed(list(commands.values())))
    return ordered[:limit]


def session_calls(workspace_root: Path, transcript: Path) -> list[dict[str, Any]]:
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        like = re.sub(r"([\\%_])", r"\\\1", str(transcript)) + ":%"
        rows = con.execute(
            "SELECT session, tool, command, text, result, ok, src FROM msgs WHERE kind = 'call' AND src LIKE ? ESCAPE '\\' ORDER BY rowid",
            (like,),
        ).fetchall()
    finally:
        con.close()
    return [dict(zip(("session", "tool", "command", "text", "result", "ok", "src"), r)) for r in rows]


def latest_transcript(workspace_root: Path, config: dict[str, Any]) -> Path | None:
    files = [p for _h, p in transcript_files(workspace_root, config)]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def capture_session(workspace_root: Path, transcript: Path | None = None, force: bool = False) -> dict[str, Any]:
    """Gera candidatos a fato do transcript (padrão: o mais recente). Respeita `[recall].capture_on_stop` salvo `force`."""
    config = load_config(workspace_root)
    if not force and not config["recall"]["capture_on_stop"]:
        return {"status": "disabled", "added": 0}
    transcript = transcript or latest_transcript(workspace_root, config)
    if transcript is None or not transcript.is_file():
        return {"status": "no_transcript", "added": 0}
    sync_recall(workspace_root, budget_s=5.0)
    privacy = Privacy(load_raw_policy(workspace_root), config["recall"]["redact"], workspace_root)
    runner = load_raw_policy(workspace_root).get("ctx", {}).get("runner", ".venv/bin/ctx")
    candidates = extract_candidates(session_calls(workspace_root, transcript), config, privacy, runner)
    for c in candidates:
        c["text"] = privacy.clean(c["text"], 400)
    added = add_candidates(workspace_root, candidates)
    return {"status": "ok", "found": len(candidates), "added": added, "transcript": str(transcript)}


def capture_from_hook(workspace_root: Path, stdin_text: str) -> dict[str, Any]:
    """Entrada do hook de parada: lê o JSON do evento (transcript_path) e captura. Nunca levanta exceção."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        raw = payload.get("transcript_path") if isinstance(payload, dict) else None
        transcript = Path(raw).expanduser() if isinstance(raw, str) and raw else None
        return capture_session(workspace_root, transcript)
    except Exception as e:  # noqa: BLE001 - hook de parada nunca pode atrapalhar o harness
        return {"status": "error", "error": str(e), "added": 0}
