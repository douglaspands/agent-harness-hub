"""Fatos verificados (`ctx fact add|search|stale|review|rm`): valores medidos, decisões e armadilhas, com fonte e data.

Cada fato exige uma fonte verificável no momento do registro: `arquivo[:linha]` (existe; a linha existe) ou uma URL.
Para fontes de arquivo guardamos o hash do trecho citado (ou do arquivo), e `ctx fact stale` lista os fatos cuja fonte
mudou depois (ou sumiu). URLs não são checadas (nenhuma rede): aparecem à parte, com a data do registro.

Candidatos vindos da captura automática de fim de sessão ficam em `fact_candidates` e só viram fato após
`ctx fact review --accept ...` (revisão humana). Nada aqui sai da máquina; `paths.deny` é respeitado e padrões de
segredo são redigidos.

100% biblioteca padrão.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from ctx.config import is_denied, load_config, load_raw_policy
from ctx.memory_db import checkpoint_memory, open_memory
from ctx.recall import Privacy

URL_SOURCE_RE = re.compile(r"^https?://\S+$")
FILE_HASH_CAP = 8 * 1024 * 1024
MAX_FACT_CHARS = 1000


class FactError(Exception):
    """Fato ou fonte inválidos (mensagem pronta para o usuário)."""


def normalize_tags(tags: str | list[str] | None) -> str:
    raw = tags.split(",") if isinstance(tags, str) else (tags or [])
    clean = sorted({re.sub(r"\s+", "-", t.strip().lower()) for t in raw if t.strip()})
    return ("," + ",".join(clean) + ",") if clean else ""


def show_tags(stored: str) -> list[str]:
    return [t for t in stored.split(",") if t]


def _line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="replace")).hexdigest()


def _file_hash(path: Path) -> str:
    data = path.read_bytes()[:FILE_HASH_CAP]
    return hashlib.sha256(data).hexdigest()


def parse_source(source: str, workspace_root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Valida a fonte e captura o hash do trecho. Retorna os campos de `facts`."""
    source = source.strip()
    if URL_SOURCE_RE.match(source):
        return {"source": source, "source_kind": "url", "source_path": None, "source_line": None, "source_hash": None}
    path_part, line = source, None
    m = re.match(r"^(.*):(\d+)$", source)
    if m:
        path_part, line = m.group(1), int(m.group(2))
    path = Path(path_part).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    path = path.resolve()
    if is_denied(path, policy, workspace_root):
        raise FactError(f"fonte em caminho negado pela política (paths.deny): {source}")
    if not path.is_file():
        raise FactError(f"fonte não encontrada: {source} (use arquivo[:linha] existente ou uma URL https)")
    if line is not None:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not 1 <= line <= len(lines):
            raise FactError(f"linha {line} não existe em {path_part} (tem {len(lines)} linhas)")
        digest = _line_hash(lines[line - 1])
    else:
        digest = _file_hash(path)
    return {"source": source, "source_kind": "file", "source_path": str(path), "source_line": line, "source_hash": digest}


def add_fact(workspace_root: Path, text: str, source: str, tags: str | list[str] | None = None) -> dict[str, Any]:
    config = load_config(workspace_root)
    policy = load_raw_policy(workspace_root)
    text = text.strip()
    if not text:
        raise FactError("fato vazio")
    if not source or not source.strip():
        raise FactError("--source é obrigatório (arquivo:linha ou URL): fato sem fonte não é verificável")
    privacy = Privacy(policy, config["recall"]["redact"], workspace_root)
    if privacy.blocked(text):
        raise FactError("o texto cita um caminho negado pela política (paths.deny); reescreva sem ele")
    text = privacy.clean(text, MAX_FACT_CHARS)
    src = parse_source(source, workspace_root, policy)
    tag_str = normalize_tags(tags)
    con = open_memory(workspace_root, config["index"])
    try:
        with con:
            dup = con.execute("SELECT id, tags FROM facts WHERE text = ? AND source = ?", (text, src["source"])).fetchone()
            if dup:
                merged = normalize_tags(show_tags(dup[1]) + show_tags(tag_str))
                con.execute("UPDATE facts SET tags = ? WHERE id = ?", (merged, dup[0]))
                con.execute("UPDATE facts_fts SET tags = ? WHERE rowid = ?", (merged.replace(",", " "), dup[0]))
                return {"id": dup[0], "duplicate": True, **src}
            cur = con.execute(
                "INSERT INTO facts (text, source, source_kind, source_path, source_line, source_hash, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (text, src["source"], src["source_kind"], src["source_path"], src["source_line"], src["source_hash"], tag_str, time.time()),
            )
            con.execute(
                "INSERT INTO facts_fts (rowid, text, tags, source) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, text, tag_str.replace(",", " "), src["source"]),
            )
            return {"id": cur.lastrowid, "duplicate": False, **src}
    finally:
        con.close()
        checkpoint_memory(workspace_root)


def fact_status(fact: dict[str, Any]) -> tuple[str, str]:
    """(status, detalhe): url | ok | stale | missing."""
    if fact["source_kind"] == "url":
        return "url", "fonte web (não verificada, sem rede)"
    path = Path(fact["source_path"])
    if not path.is_file():
        return "missing", "arquivo-fonte removido"
    try:
        if fact["source_line"]:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if fact["source_line"] > len(lines):
                return "stale", f"linha {fact['source_line']} não existe mais"
            current = _line_hash(lines[fact["source_line"] - 1])
        else:
            current = _file_hash(path)
    except OSError:
        return "missing", "fonte ilegível"
    if current != fact["source_hash"]:
        return "stale", "o trecho citado mudou" if fact["source_line"] else "o arquivo mudou"
    return "ok", "fonte inalterada"


COLUMNS = "id, text, source, source_kind, source_path, source_line, source_hash, tags, created_at"


def _rows(con: sqlite3.Connection, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    cols = [c.strip() for c in COLUMNS.split(",")]
    out = []
    for row in con.execute(sql, params).fetchall():
        fact = dict(zip(cols, row))
        fact["tags"] = show_tags(fact["tags"])
        fact["when"] = time.strftime("%Y-%m-%d", time.localtime(fact["created_at"]))
        fact["status"], fact["status_detail"] = fact_status(fact)
        out.append(fact)
    return out


def search_facts(workspace_root: Path, query: str = "", tag: str | None = None, top: int = 10) -> list[dict[str, Any]]:
    terms = re.findall(r"[\wÀ-ſ][\wÀ-ſ.+-]*", query)
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        params: list[Any] = []
        if terms:
            sql = f"SELECT {', '.join('f.' + c.strip() for c in COLUMNS.split(','))} FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid WHERE facts_fts MATCH ?"
            params.append(" OR ".join('"' + t.replace('"', "") + '"' for t in terms))
            if tag:
                sql += " AND f.tags LIKE ?"
                params.append(f"%,{normalize_tags(tag).strip(',')},%")
            sql += " ORDER BY bm25(facts_fts) LIMIT ?"
        else:
            sql = f"SELECT {COLUMNS} FROM facts"
            if tag:
                sql += " WHERE tags LIKE ?"
                params.append(f"%,{normalize_tags(tag).strip(',')},%")
            sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(top)
        return _rows(con, sql, params)
    finally:
        con.close()


def stale_facts(workspace_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Fatos cuja fonte mudou/sumiu depois do registro; URLs (não verificáveis) listadas à parte."""
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        facts = _rows(con, f"SELECT {COLUMNS} FROM facts ORDER BY created_at DESC", [])
    finally:
        con.close()
    return {
        "stale": [f for f in facts if f["status"] in ("stale", "missing")],
        "unverifiable": [f for f in facts if f["status"] == "url"],
        "ok": [f for f in facts if f["status"] == "ok"],
    }


def remove_fact(workspace_root: Path, fact_id: int) -> bool:
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        with con:
            n = con.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount
            con.execute("DELETE FROM facts_fts WHERE rowid = ?", (fact_id,))
        return n > 0
    finally:
        con.close()


# ----------------------------------------------------------------------------------------------
# Candidatos (captura automática) e revisão humana
# ----------------------------------------------------------------------------------------------


def candidate_digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip().lower()).encode()).hexdigest()


def add_candidates(workspace_root: Path, candidates: list[dict[str, Any]]) -> int:
    """Grava candidatos pendentes (deduplicados por texto, inclusive contra fatos e candidatos já decididos)."""
    config = load_config(workspace_root)
    con = open_memory(workspace_root, config["index"])
    added = 0
    try:
        with con:
            for c in candidates:
                if con.execute("SELECT 1 FROM facts WHERE text = ?", (c["text"],)).fetchone():
                    continue
                cur = con.execute(
                    "INSERT OR IGNORE INTO fact_candidates (text, source, tags, session, created_at, status, digest) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (c["text"], c["source"], normalize_tags(c.get("tags")), c.get("session", ""), time.time(), candidate_digest(c["text"])),
                )
                added += cur.rowcount
    finally:
        con.close()
        checkpoint_memory(workspace_root)
    return added


def list_candidates(workspace_root: Path, status: str = "pending") -> list[dict[str, Any]]:
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        rows = con.execute(
            "SELECT id, text, source, tags, session, created_at, status FROM fact_candidates WHERE status = ? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"id": r[0], "text": r[1], "source": r[2], "tags": show_tags(r[3]), "session": r[4],
         "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[5])), "status": r[6]}
        for r in rows
    ]


def review_candidates(workspace_root: Path, accept: list[int], reject: list[int]) -> list[dict[str, Any]]:
    """Aceita (vira fato, fonte = linha do transcript) ou rejeita candidatos. Retorna um relatório por id."""
    report: list[dict[str, Any]] = []
    pending = {c["id"]: c for c in list_candidates(workspace_root)}
    con = open_memory(workspace_root, load_config(workspace_root)["index"])
    try:
        for cid in reject:
            if cid in pending:
                with con:
                    con.execute("UPDATE fact_candidates SET status = 'rejected' WHERE id = ?", (cid,))
                report.append({"id": cid, "result": "rejeitado"})
            else:
                report.append({"id": cid, "result": "não encontrado (ou já decidido)"})
    finally:
        con.close()
    for cid in accept:
        c = pending.get(cid)
        if not c:
            report.append({"id": cid, "result": "não encontrado (ou já decidido)"})
            continue
        try:
            fact = add_fact(workspace_root, c["text"], c["source"], c["tags"])
        except FactError as e:
            report.append({"id": cid, "result": f"não aceito: {e}"})
            continue
        con = open_memory(workspace_root, load_config(workspace_root)["index"])
        try:
            with con:
                con.execute("UPDATE fact_candidates SET status = 'accepted' WHERE id = ?", (cid,))
        finally:
            con.close()
        report.append({"id": cid, "result": f"aceito como fato #{fact['id']}"})
    return report
