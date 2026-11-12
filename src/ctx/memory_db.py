"""Banco da memória local (`.ctx/memory.db`): índice dos transcripts (recall) e fatos verificados (fact).

Separado do índice de código (`index.db`) de propósito:
- o índice de código é cache: pode ser apagado e recriado a qualquer momento;
- aqui moram FATOS e candidatos, que são dados do usuário: nunca são apagados por migração, reparo ou `ctx index --force`.
  Só as tabelas de transcript (`msgs`, `transcript_files`, `call_ids`) são reconstruíveis. Banco ilegível é movido para
  `memory.db.corrupt-<data>` (nunca apagado) e um novo é criado.

Tudo local: nenhuma rede, nenhum envio.

100% biblioteca padrão.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ctx.config import DEFAULTS

MEMORY_SCHEMA_VERSION = 1
TOKENIZE = "tokenize='unicode61 remove_diacritics 2'"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS transcript_files (
    path TEXT PRIMARY KEY, harness TEXT, mtime_ns INTEGER, size INTEGER, offset INTEGER, lines INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(
    text, command, result,
    session UNINDEXED, ts UNINDEXED, kind UNINDEXED, tool UNINDEXED, ok UNINDEXED, src UNINDEXED,
    {TOKENIZE}
);
CREATE TABLE IF NOT EXISTS call_ids (tool_use_id TEXT PRIMARY KEY, msg_rowid INTEGER);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL, source TEXT NOT NULL, source_kind TEXT NOT NULL,
    source_path TEXT, source_line INTEGER, source_hash TEXT,
    tags TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, tags, source, {TOKENIZE});
CREATE TABLE IF NOT EXISTS fact_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL, source TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', session TEXT,
    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', digest TEXT UNIQUE
);
"""


def memory_path(workspace_root: Path) -> Path:
    return workspace_root / ".ctx" / "memory.db"


def _connect(path: Path, idx_cfg: dict[str, Any]) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=idx_cfg["busy_timeout_ms"] / 1000)
    try:
        con.execute(f"PRAGMA busy_timeout={int(idx_cfg['busy_timeout_ms'])}")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute(f"PRAGMA journal_size_limit={int(idx_cfg['journal_size_limit_mb']) * 1024 * 1024}")
        con.execute(f"PRAGMA wal_autocheckpoint={int(idx_cfg['wal_autocheckpoint_pages'])}")
        con.executescript(SCHEMA)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != MEMORY_SCHEMA_VERSION:
            # Migrações futuras são ADITIVAS (ALTER ... ADD COLUMN); facts/fact_candidates nunca são recriadas.
            con.execute(f"PRAGMA user_version={MEMORY_SCHEMA_VERSION}")
            con.commit()
        return con
    except BaseException:
        con.close()
        raise


def open_memory(workspace_root: Path, idx_cfg: dict[str, Any] | None = None) -> sqlite3.Connection:
    """Abre (criando) a memória. Banco corrompido é preservado como `.corrupt-<data>` e substituído."""
    cfg = {**DEFAULTS["index"], **(idx_cfg or {})}
    path = memory_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _connect(path, cfg)
    except sqlite3.DatabaseError:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for f in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if f.exists():
                f.rename(f.with_name(f.name + f".corrupt-{stamp}"))
        return _connect(path, cfg)


def checkpoint_memory(workspace_root: Path) -> None:
    path = memory_path(workspace_root)
    if not path.exists():
        return
    try:
        con = sqlite3.connect(str(path), timeout=2)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError:
        pass
