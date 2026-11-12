"""Recall: memória consultável entre sessões, a partir dos transcripts locais do agente (`ctx recall "<pergunta>"`).

Indexa por MENSAGEM e por CHAMADA DE FERRAMENTA (comando, resultado truncado, sucesso/erro, data) em FTS5, de forma
incremental (só lê o que foi acrescentado ao .jsonl) e sem regex pesada: parse JSON linha a linha + busca FTS5.
O caminho dos transcripts de cada harness vem de `[recall].sources` (padrão: ~/.claude/projects/<projeto>/*.jsonl).

Privacidade: tudo local. Entradas que tocam caminhos de `paths.deny` nunca são gravadas (e são filtradas de novo na
consulta, caso a política tenha mudado); padrões de segredo (`[recall].redact`) são trocados por [REDACTED].

100% biblioteca padrão.
"""

from __future__ import annotations

import glob as globlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from ctx.config import load_config, load_raw_policy
from ctx.memory_db import checkpoint_memory, open_memory

URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "para", "por",
    "com", "que", "qual", "quais", "foi", "fui", "usada", "usado", "usei", "e", "ou", "se", "ao", "à", "eu", "meu",
    "minha", "ja", "já", "the", "of", "to", "in", "on", "for", "and", "or", "is", "was", "what", "which", "did", "i",
    "my", "ultimo", "último", "ultima", "última", "recente", "mais", "comando", "comandos", "certo", "sucesso",
    "funcionou", "deu", "erro", "falhou", "url", "link", "endereço", "last", "latest", "command", "worked",
    "succeeded", "failed", "error", "usado", "usada", "rodei", "executei", "baixei",
}
OK_WORDS = ("deu certo", "funcionou", "com sucesso", "bem-sucedido", "bem sucedido", "worked", "succeeded", "sucesso")
ERR_WORDS = ("deu erro", "falhou", "falha", "failed", "error", "erro")
RECENT_WORDS = ("último", "ultimo", "última", "ultima", "mais recente", "latest", "last", "recente")
URL_WORDS = ("url", "link", "endereço", "endereco")
COMMAND_WORDS = ("comando", "command", "rodei", "executei")


# ----------------------------------------------------------------------------------------------
# Parser de transcripts (um por harness; hoje: Claude Code .jsonl)
# ----------------------------------------------------------------------------------------------


@dataclass
class Row:
    kind: str  # user | assistant | call
    text: str = ""
    command: str = ""
    result: str = ""
    session: str = ""
    ts: float = 0.0
    tool: str = ""
    ok: int | None = None
    src: str = ""
    tool_use_id: str = ""


@dataclass
class Result:
    tool_use_id: str
    text: str
    ok: int


def _ts(value: Any) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def tool_summary(name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """(comando, descrição) legíveis de uma chamada de ferramenta, sem despejar o input inteiro."""
    desc = str(tool_input.get("description") or "")
    for key in ("command", "url", "file_path", "path", "pattern", "query", "notebook_path"):
        if tool_input.get(key):
            return str(tool_input[key]), desc
    return json.dumps(tool_input, ensure_ascii=False)[:300], desc


def parse_claude_entry(entry: dict[str, Any], session_default: str, src: str) -> list[Row | Result]:
    out: list[Row | Result] = []
    message = entry.get("message")
    if not isinstance(message, dict) or entry.get("type") not in ("user", "assistant"):
        return out
    role = message.get("role") or entry["type"]
    session = str(entry.get("sessionId") or session_default)
    ts = _ts(entry.get("timestamp"))
    content = message.get("content")
    if isinstance(content, str):
        if content.strip() and not entry.get("isMeta"):
            out.append(Row(kind=role, text=content, session=session, ts=ts, src=src))
        return out
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            out.append(Row(kind=role, text=str(block["text"]), session=session, ts=ts, src=src))
        elif btype == "tool_use":
            command, desc = tool_summary(str(block.get("name", "")), block.get("input") or {})
            out.append(
                Row(kind="call", tool=str(block.get("name", "")), command=command, text=desc, session=session, ts=ts,
                    src=src, tool_use_id=str(block.get("id", "")))
            )
        elif btype == "tool_result":
            text = block.get("content")
            text = _blocks_text(text) if not isinstance(text, str) else text
            failed = bool(block.get("is_error")) or bool(re.match(r"\s*Exit code [1-9]", text or ""))
            out.append(Result(tool_use_id=str(block.get("tool_use_id", "")), text=text or "", ok=0 if failed else 1))
    return out


PARSERS: dict[str, Callable[[dict[str, Any], str, str], list[Row | Result]]] = {"claude": parse_claude_entry}


# ----------------------------------------------------------------------------------------------
# Privacidade: caminhos negados e segredos
# ----------------------------------------------------------------------------------------------


class Privacy:
    """Filtro de `paths.deny` + redação de segredos, construído uma vez por execução."""

    def __init__(self, policy: dict[str, Any], redact: list[str], workspace_root: Path) -> None:
        self.needles: list[str] = []
        self.name_res: list[re.Pattern[str]] = []
        for entry in policy.get("paths", {}).get("deny", []):
            if "/" not in entry and not entry.startswith("~"):
                self.name_res.append(re.compile(r"(?<![\w.-])" + re.escape(entry) + r"(?![\w.-])"))
            else:
                expanded = os.path.expanduser(entry)
                if not os.path.isabs(expanded):
                    expanded = str(workspace_root / expanded)
                self.needles += [entry.rstrip("/"), expanded.rstrip("/")]
        self.redact: list[re.Pattern[str]] = []
        for pat in redact:
            try:
                self.redact.append(re.compile(pat))
            except re.error:
                continue

    def blocked(self, *texts: str) -> bool:
        for text in texts:
            if not text:
                continue
            if any(n in text for n in self.needles) or any(r.search(text) for r in self.name_res):
                return True
        return False

    def clean(self, text: str, limit: int) -> str:
        if len(text) > limit * 4:  # limita ANTES dos padrões (custo), preservando o começo e o fim (onde ficam os erros)
            text = text[: limit * 2] + " … " + text[-limit * 2 :]
        for rx in self.redact:
            text = rx.sub("[REDACTED]", text)
        if len(text) > limit:
            half = limit // 2
            text = text[:half] + " … " + text[-half:]
        return text


# ----------------------------------------------------------------------------------------------
# Indexação incremental
# ----------------------------------------------------------------------------------------------


def project_slug(workspace_root: Path) -> str:
    """Mesmo nome de pasta que o Claude Code usa para o projeto (~/.claude/projects/<slug>)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace_root.resolve()))


def transcript_files(workspace_root: Path, config: dict[str, Any]) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for source in config["recall"]["sources"]:
        harness = source.get("harness", "claude")
        pattern = os.path.expanduser(str(source.get("path", "")).replace("{project}", project_slug(workspace_root)))
        for match in sorted(globlib.glob(pattern)):
            if os.path.isfile(match):
                found.append((harness, Path(match)))
    return found


def _read_new_lines(path: Path, offset: int, max_line: int) -> Iterator[tuple[bytes, int]]:
    """Linhas COMPLETAS a partir de `offset`, com o offset do fim de cada uma (linha parcial fica para depois)."""
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            pos += len(raw)
            yield raw, pos


def sync_file(con: sqlite3.Connection, harness: str, path: Path, privacy: Privacy, config: dict[str, Any], deadline: float | None) -> int:
    parser = PARSERS.get(harness)
    if parser is None:
        return 0
    st = path.stat()
    row = con.execute("SELECT mtime_ns, size, offset, lines FROM transcript_files WHERE path = ?", (str(path),)).fetchone()
    offset, lines_done = 0, 0
    if row:
        if row[1] == st.st_size and row[0] == st.st_mtime_ns:
            return 0
        if st.st_size >= row[2]:
            offset, lines_done = row[2], row[3]
        else:  # arquivo reescrito/encolheu: reindexa do zero
            con.execute("DELETE FROM msgs WHERE src LIKE ? ESCAPE '\\'", (re.sub(r"([\\%_])", r"\\\1", str(path)) + ":%",))
    limit = int(config["recall"]["result_chars"])
    max_line = int(config["recall"]["max_line_kb"]) * 1024
    added = 0
    new_offset = offset
    lineno = lines_done
    for raw, pos in _read_new_lines(path, offset, max_line):
        lineno += 1
        new_offset = pos
        if len(raw) > max_line:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for item in parser(entry, path.stem, f"{path}:{lineno}"):
            if isinstance(item, Row):
                added += _store_row(con, item, privacy, limit)
            else:
                _store_result(con, item, privacy, limit)
        if deadline is not None and time.perf_counter() > deadline:
            break
    con.execute(
        "INSERT OR REPLACE INTO transcript_files (path, harness, mtime_ns, size, offset, lines) VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), harness, st.st_mtime_ns, st.st_size, new_offset, lineno),
    )
    return added


def _store_row(con: sqlite3.Connection, row: Row, privacy: Privacy, limit: int) -> int:
    if row.kind == "call" and privacy.blocked(row.command, row.text):
        return 0  # a chamada toca um caminho negado: não entra no índice
    cur = con.execute(
        "INSERT INTO msgs (text, command, result, session, ts, kind, tool, ok, src) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (privacy.clean(row.text, 2000), privacy.clean(row.command, 1000), "", row.session, row.ts, row.kind, row.tool, row.ok, row.src),
    )
    if row.tool_use_id:
        con.execute("INSERT OR REPLACE INTO call_ids (tool_use_id, msg_rowid) VALUES (?, ?)", (row.tool_use_id, cur.lastrowid))
    return 1


def _store_result(con: sqlite3.Connection, res: Result, privacy: Privacy, limit: int) -> None:
    found = con.execute("SELECT msg_rowid FROM call_ids WHERE tool_use_id = ?", (res.tool_use_id,)).fetchone()
    if not found:
        return  # chamada não indexada (ex.: negada pela política)
    if privacy.blocked(res.text):
        con.execute("DELETE FROM msgs WHERE rowid = ?", (found[0],))
        con.execute("DELETE FROM call_ids WHERE tool_use_id = ?", (res.tool_use_id,))
        return
    con.execute("UPDATE msgs SET result = ?, ok = ? WHERE rowid = ?", (privacy.clean(res.text, limit), res.ok, found[0]))


def sync_recall(workspace_root: Path, budget_s: float | None = None, rebuild: bool = False) -> dict[str, Any]:
    """Atualiza o índice de recall com o que foi acrescentado aos transcripts desde a última vez."""
    start = time.perf_counter()
    config = load_config(workspace_root)
    policy = load_raw_policy(workspace_root)
    privacy = Privacy(policy, config["recall"]["redact"], workspace_root)
    budget = float(config["recall"]["sync_budget_s"] if budget_s is None else budget_s)
    deadline = start + budget
    con = open_memory(workspace_root, config["index"])
    files = transcript_files(workspace_root, config)
    unknown = sorted({h for h, _ in files if h not in PARSERS})
    added = scanned = 0
    partial = False
    try:
        if rebuild:
            con.execute("DELETE FROM msgs")
            con.execute("DELETE FROM call_ids")
            con.execute("DELETE FROM transcript_files")
        with con:
            for harness, path in files:
                if time.perf_counter() > deadline:
                    partial = True
                    break
                scanned += 1
                try:
                    added += sync_file(con, harness, path, privacy, config, deadline)
                except OSError:
                    continue
    finally:
        con.close()
    checkpoint_memory(workspace_root)
    return {
        "files": len(files),
        "scanned": scanned,
        "added": added,
        "partial": partial or time.perf_counter() > deadline,
        "unsupported_harnesses": unknown,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


# ----------------------------------------------------------------------------------------------
# Consulta
# ----------------------------------------------------------------------------------------------


@dataclass
class Intent:
    terms: list[str] = field(default_factory=list)
    only_ok: bool = False
    only_err: bool = False
    recent: bool = False
    want_urls: bool = False
    commands: bool = False


def interpret(question: str) -> Intent:
    """Heurística simples da pergunta (pt/en): sucesso/erro, 'último', URL, 'comando' + termos úteis."""
    q = question.lower()
    intent = Intent(
        only_ok=any(w in q for w in OK_WORDS),
        only_err=any(w in q for w in ERR_WORDS) and not any(w in q for w in OK_WORDS),
        recent=any(w in q for w in RECENT_WORDS),
        want_urls=any(re.search(rf"\b{w}\b", q) for w in URL_WORDS),
        commands=any(w in q for w in COMMAND_WORDS),
    )
    seen: list[str] = []
    for tok in re.findall(r"[\wÀ-ſ][\wÀ-ſ.+-]*", question):
        low = tok.lower()
        if low in STOPWORDS or low in seen:
            continue
        seen.append(low)
    intent.terms = seen
    return intent


def fts_query(terms: list[str]) -> str:
    quoted = ['"' + t.replace('"', "") + '"' for t in terms if t.replace('"', "")]
    return " OR ".join(quoted)


def parse_since(value: str | None) -> float:
    if not value:
        return 0.0
    m = re.fullmatch(r"(\d+)([hdw])", value.strip().lower())
    if not m:
        raise ValueError(f"--since inválido: {value!r} (ex.: 12h, 7d, 2w)")
    seconds = int(m.group(1)) * {"h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return time.time() - seconds


def recall(
    workspace_root: Path,
    question: str,
    top: int = 5,
    only_ok: bool | None = None,
    tool: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    order: str | None = None,
    sync: bool = True,
    raw: bool = False,
) -> dict[str, Any]:
    start = time.perf_counter()
    config = load_config(workspace_root)
    sync_info = sync_recall(workspace_root) if sync else None
    intent = Intent(terms=re.findall(r"[\wÀ-ſ][\wÀ-ſ.+-]*", question)) if raw else interpret(question)
    if only_ok is None:
        only_ok = intent.only_ok
    if intent.commands and not kind:
        kind = "call"
    policy = load_raw_policy(workspace_root)
    privacy = Privacy(policy, config["recall"]["redact"], workspace_root)

    con = open_memory(workspace_root, config["index"])
    try:
        where, params = [], []
        if intent.terms:
            where.append("msgs MATCH ?")
            params.append(fts_query(intent.terms))
        if only_ok:
            where.append("ok = 1")
        elif intent.only_err:
            where.append("ok = 0")
        if tool:
            where.append("tool = ?")
            params.append(tool)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if intent.want_urls:
            where.append("(command LIKE '%http%' OR text LIKE '%http%' OR result LIKE '%http%')")
        since_ts = parse_since(since)
        if since_ts:
            where.append("ts >= ?")
            params.append(since_ts)
        by_recent = (order or ("recent" if intent.recent else "rank")) == "recent" or not intent.terms  # bm25 exige MATCH
        sql = "SELECT rowid, session, ts, kind, tool, ok, src, text, command, result, bm25(msgs) FROM msgs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        elif not intent.terms:
            sql += " WHERE 1=1"
        sql += " ORDER BY ts DESC" if by_recent else " ORDER BY bm25(msgs)"
        sql += " LIMIT ?"
        params.append(max(top * 4, 20))
        try:
            rows = con.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        con.close()

    hits: list[dict[str, Any]] = []
    for rowid, session, ts, k, tl, ok, src, text, command, result, _rank in rows:
        if privacy.blocked(command, text, result):
            continue  # política mudou depois da indexação
        urls = list(dict.fromkeys(URL_RE.findall(" ".join((command, text, result))))) if intent.want_urls else []
        if intent.want_urls and not urls:
            continue
        hits.append(
            {
                "session": session, "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?", "ts": ts,
                "kind": k, "tool": tl, "ok": None if ok is None else bool(ok), "src": src,
                "text": text[:300], "command": command, "result": result, "urls": urls,
            }
        )
        if len(hits) >= top:
            break
    return {
        "question": question,
        "intent": {"only_ok": only_ok, "only_err": intent.only_err, "recent": by_recent, "urls": intent.want_urls, "terms": intent.terms},
        "hits": hits,
        "sync": sync_info,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }
