"""Motor de RAG local One-Shot baseado em SQLite FTS5/BM25 e AST/Markdown Chunking.

Inclui:
- Busca multi-query com preview de N linhas e filtro por subcaminho (`ctx search`).
- Mapa estrutural multi-alvo que oculta `openspec/changes/archive/` (`ctx map`).
- Visão consolidada One-Shot: Git + OpenSpec + saúde do índice/cache (`ctx status`).

Zero dependências externas (100% biblioteca padrão: sqlite3, ast, json, pathlib, subprocess, time).
"""

from __future__ import annotations

import ast
import fnmatch
import glob as globlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ctx import WORKSPACE_ROOT
from ctx.config import DEFAULTS, is_denied, load_config, load_raw_policy

ARCHIVE_PREFIX = "openspec/changes/archive/"
MAP_PRIORITY = ("src/", "scripts/", "tests/", "openspec/changes/")
DEFAULT_PREVIEW_LINES = 25

# Esquema do índice. O índice é cache reconstruível: versão diferente => recriado e reindexado (ver get_db).
# 3: coluna `name` (nome do arquivo), tabela de trigramas `chunks_tri` e `vectors` (semântica opcional).
SCHEMA_VERSION = 3
# Pesos do bm25() por coluna de `chunks`: path, title, chunk_type, start_line, end_line, content, kind, name.
# O título carrega o símbolo (função, nó DTS, seção) e o nome do arquivo identifica o módulo: valem mais que o corpo.
BM25_WEIGHTS = (2.0, 6.0, 0.5, 0.0, 0.0, 1.0, 0.0, 4.0)
RRF_K = 60  # reciprocal rank fusion entre as listas (BM25, semântica)
HYBRID_POOL = 20  # candidatos de cada lista antes da fusão
NAME_BONUS = 0.25  # termo da consulta igual ao nome do arquivo (`push` -> push.py) vale +25% (M-26)


def name_bonus(path: str, terms: list[str]) -> float:
    """Multiplicador do resultado: 1 + NAME_BONUS quando um termo é o nome do arquivo sem extensão."""
    stem = fold(Path(path).stem)
    return 1.0 + NAME_BONUS if stem and any(fold(t) == stem for t in terms) else 1.0
KINDS = ("code", "reference", "upstream")
# WAL acima disso não vale a pena consolidar na hora de uma busca: o índice é recriado do zero.
WAL_REBUILD_BYTES = 1024 * 1024 * 1024

IGNORED_DIRS = {
    ".git",
    ".ctx",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}

IGNORED_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".webp",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".bin",
    ".tar",
    ".gz",
    ".zip",
    ".whl",
}


@dataclass
class Chunk:
    path: str
    title: str
    chunk_type: str
    start_line: int
    end_line: int
    content: str


def db_path_for(workspace_root: Path) -> Path:
    """Caminho do índice FTS5 do workspace informado (cada workspace tem o próprio .ctx/index.db)."""
    return workspace_root / ".ctx" / "index.db"


def db_files(db_path: Path) -> list[Path]:
    return [db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]


def wal_bytes(db_path: Path) -> int:
    try:
        return db_path.with_name(db_path.name + "-wal").stat().st_size
    except OSError:
        return 0


def remove_db(db_path: Path) -> None:
    """Apaga o índice (cache reconstruível) com seu WAL/SHM."""
    for f in db_files(db_path):
        try:
            f.unlink()
        except OSError:
            pass


def _apply_pragmas(con: sqlite3.Connection, idx_cfg: dict[str, Any]) -> None:
    # journal_size_limit trunca o -wal após cada checkpoint; autocheckpoint impede o crescimento sem fim.
    con.execute(f"PRAGMA busy_timeout={int(idx_cfg['busy_timeout_ms'])};")
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute(f"PRAGMA journal_size_limit={int(idx_cfg['journal_size_limit_mb']) * 1024 * 1024};")
    con.execute(f"PRAGMA wal_autocheckpoint={int(idx_cfg['wal_autocheckpoint_pages'])};")


def _create_schema(con: sqlite3.Connection) -> bool:
    """Cria/migra o esquema. Retorna True se o índice foi recriado (versão anterior ou desconhecida)."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    rebuilt = False
    if version != SCHEMA_VERSION:
        has_old = con.execute("SELECT 1 FROM sqlite_master WHERE name IN ('chunks','indexed_files') LIMIT 1").fetchone()
        # Índice de versão anterior: o FTS5 mudou de colunas, então recria (reindex automático depois).
        con.execute("DROP TABLE IF EXISTS chunks")
        con.execute("DROP TABLE IF EXISTS chunks_tri")
        con.execute("DROP TABLE IF EXISTS vectors")
        con.execute("DROP TABLE IF EXISTS indexed_files")
        con.execute("DROP TABLE IF EXISTS meta")
        rebuilt = bool(has_old)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS indexed_files (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER,
            size INTEGER,
            hash TEXT,
            kind TEXT NOT NULL DEFAULT 'code'
        );
        """
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            path,
            title,
            chunk_type,
            start_line UNINDEXED,
            end_line UNINDEXED,
            content,
            kind UNINDEXED,
            name,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    # Mesmo rowid de `chunks`: trigramas acham pedaços de identificador ("dsidph", "lane_mb"). SQLite < 3.34 não
    # tem o tokenizador trigram: a tabela fica de fora e a busca pula esse passo do recuo.
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(path UNINDEXED, title, content, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        pass
    # Embeddings (float32 normalizados) por rowid de `chunks`; só com o extra `semantic` (ver ctx/semantic.py).
    con.execute("CREATE TABLE IF NOT EXISTS vectors (id INTEGER PRIMARY KEY, path TEXT NOT NULL, vec BLOB NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS vectors_path ON vectors(path)")
    if version != SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
    return rebuilt


def get_db(db_path: Path | None = None, idx_cfg: dict[str, Any] | None = None) -> sqlite3.Connection:
    """Abre (leitura/escrita) o índice em modo WAL, migrando o esquema se necessário.

    Banco corrompido ou ilegível é apagado e recriado: o índice é cache, nunca fonte de verdade.
    """
    db_path = db_path or db_path_for(WORKSPACE_ROOT)
    idx_cfg = {**DEFAULTS["index"], **(idx_cfg or {})}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        con = sqlite3.connect(str(db_path), timeout=idx_cfg["busy_timeout_ms"] / 1000)
        try:
            _apply_pragmas(con, idx_cfg)
            _create_schema(con)
            return con
        except sqlite3.DatabaseError:
            con.close()
            if attempt == 2:
                raise
            remove_db(db_path)
    raise AssertionError("inalcançável")  # pragma: no cover


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Conexão somente leitura (`mode=ro`): buscas nunca escrevem no índice."""
    uri = "file:" + db_path.as_posix().replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=DEFAULTS["index"]["busy_timeout_ms"] / 1000)


def index_state(db_path: Path) -> str:
    """'missing' | 'outdated' (esquema anterior) | 'ok' | 'broken'."""
    if not db_path.exists():
        return "missing"
    try:
        con = open_readonly(db_path)
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                return "outdated"
            con.execute("SELECT 1 FROM chunks LIMIT 1").fetchall()
            return "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return "broken"


def checkpoint(db_path: Path) -> dict[str, int]:
    """Consolida o WAL no banco (TRUNCATE). Retorna os tamanhos do -wal antes e depois."""
    before = wal_bytes(db_path)
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path), timeout=2)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            finally:
                con.close()
        except sqlite3.DatabaseError:
            pass
    return {"before": before, "after": wal_bytes(db_path)}


def extract_python_chunks(file_path: Path, rel_path: str, content: str) -> list[Chunk]:
    """Extrai blocos estruturados de código Python usando AST (classes, métodos e funções)."""
    chunks: list[Chunk] = []
    lines = content.splitlines()

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        # Fallback para bloco único em caso de arquivo com sintaxe incompleta
        return [
            Chunk(
                path=rel_path,
                title=f"file {file_path.name}",
                chunk_type="python_raw",
                start_line=1,
                end_line=len(lines),
                content=content,
            )
        ]

    # Docstring do módulo se existir
    docstring = ast.get_docstring(tree)
    if docstring:
        chunks.append(
            Chunk(
                path=rel_path,
                title=f"module docstring: {file_path.name}",
                chunk_type="python_doc",
                start_line=1,
                end_line=min(30, len(lines)),
                content=docstring,
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start
            node_name = node.name
            node_type = "class" if isinstance(node, ast.ClassDef) else "function"

            chunk_content = "\n".join(lines[start - 1 : end])
            chunks.append(
                Chunk(
                    path=rel_path,
                    title=f"{node_type} {node_name}",
                    chunk_type=f"python_{node_type}",
                    start_line=start,
                    end_line=end,
                    content=chunk_content,
                )
            )

            # Para classes, indexar também métodos individuais se a classe for longa
            if isinstance(node, ast.ClassDef) and (end - start) > 25:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_start = item.lineno
                        m_end = item.end_lineno if hasattr(item, "end_lineno") and item.end_lineno else m_start
                        m_content = "\n".join(lines[m_start - 1 : m_end])
                        chunks.append(
                            Chunk(
                                path=rel_path,
                                title=f"method {node.name}.{item.name}",
                                chunk_type="python_method",
                                start_line=m_start,
                                end_line=m_end,
                                content=m_content,
                            )
                        )

    if not chunks:
        # Se for um script direto sem funções/classes, indexa como bloco principal
        chunks.append(
            Chunk(
                path=rel_path,
                title=f"script {file_path.name}",
                chunk_type="python_script",
                start_line=1,
                end_line=len(lines),
                content=content,
            )
        )
    return chunks


def extract_markdown_chunks(file_path: Path, rel_path: str, content: str) -> list[Chunk]:
    """Divide arquivos Markdown por seções baseadas em títulos (#, ##, ###)."""
    chunks: list[Chunk] = []
    lines = content.splitlines()
    header_pattern = re.compile(r"^(#{1,4})\s+(.+)$")

    current_title = f"{file_path.name} (intro)"
    current_start = 1
    current_lines: list[str] = []
    in_fence = False

    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        # Linhas "# ..." dentro de blocos de código (comentários TOML/bash, snippets) não são títulos
        match = None if in_fence else header_pattern.match(line)
        if match:
            if current_lines:
                chunks += markdown_section_chunks(rel_path, current_title, current_start, current_lines)
            current_title = match.group(2).strip()
            current_start = i
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        chunks += markdown_section_chunks(rel_path, current_title, current_start, current_lines)

    return chunks


MD_MAX_LINES = 60  # seção maior que isso vira partes (M-13): seções enormes dominavam o BM25
_MD_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")


def markdown_section_chunks(rel_path: str, title: str, start: int, sec_lines: list[str]) -> list[Chunk]:
    """Um trecho por seção; se passar de MD_MAX_LINES, partes cortadas em item de lista de nível 1 ou parágrafo
    (nunca dentro de bloco de código), cada uma com o título da seção."""
    if not "\n".join(sec_lines).strip():
        return []
    if len(sec_lines) <= MD_MAX_LINES:
        return [Chunk(rel_path, title, "markdown_section", start, start + len(sec_lines) - 1, "\n".join(sec_lines).strip())]
    parts: list[tuple[int, int]] = []  # índices [a, b) em sec_lines
    a = 0
    last_cut = None
    in_fence = False
    for k, line in enumerate(sec_lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        boundary = not in_fence and k > a and (not sec_lines[k - 1].strip() or _MD_ITEM_RE.match(line))
        if boundary:
            last_cut = k
        if k - a + 1 > MD_MAX_LINES:
            cut = last_cut if last_cut and last_cut > a else k
            parts.append((a, cut))
            a, last_cut = cut, None
    parts.append((a, len(sec_lines)))
    out: list[Chunk] = []
    for n, (pa, pb) in enumerate(parts, start=1):
        body = "\n".join(sec_lines[pa:pb]).strip()
        if body:
            label = title if n == 1 else f"{title} (parte {n})"
            out.append(Chunk(rel_path, label, "markdown_section", start + pa, start + pb - 1, body))
    return out


def extract_generic_chunks(file_path: Path, rel_path: str, content: str) -> list[Chunk]:
    """Divide arquivos de texto/configuração (TOML, YAML, JSON, Shell) em blocos lógicos."""
    lines = content.splitlines()
    if not lines:
        return []

    # Para arquivos curtos (< 80 linhas), bloco único
    if len(lines) <= 80:
        return [
            Chunk(
                path=rel_path,
                title=f"{file_path.name} (full)",
                chunk_type=f"text_{file_path.suffix.lstrip('.') or 'plain'}",
                start_line=1,
                end_line=len(lines),
                content=content,
            )
        ]

    # Para arquivos maiores, fatiar em blocos de ~50 linhas
    chunks: list[Chunk] = []
    step = 50
    for start_idx in range(0, len(lines), step):
        end_idx = min(start_idx + step, len(lines))
        chunk_lines = lines[start_idx:end_idx]
        chunks.append(
            Chunk(
                path=rel_path,
                title=f"{file_path.name} (lines {start_idx + 1}-{end_idx})",
                chunk_type=f"text_{file_path.suffix.lstrip('.') or 'plain'}",
                start_line=start_idx + 1,
                end_line=end_idx,
                content="\n".join(chunk_lines),
            )
        )
    return chunks


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s*(.*)$")
SYMBOL_RES = (
    re.compile(r"^\+\s*(?:async\s+)?def\s+(\w+)"),
    re.compile(r"^\+\s*class\s+(\w+)"),
    re.compile(r"^\+\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
    re.compile(r"^\+\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
    re.compile(r"^\+\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),
    re.compile(r"^\+\s*#\s*define\s+(\w+)"),
    # C/C++/Java-like: tipo nome(args) {   (palavras de controle filtradas abaixo)
    re.compile(r"^\+[A-Za-z_][\w\s\*:<>,]*?\b(\w+)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$"),
)
NOT_SYMBOLS = {"if", "for", "while", "switch", "return", "else", "sizeof", "catch", "do"}
PATCH_HUNK_CHARS = 4000


def extract_patch_chunks(file_path: Path, rel_path: str, content: str) -> list[Chunk]:
    """Patches (.patch/.diff): um resumo (arquivos tocados, símbolos adicionados, hunks) + um chunk por hunk."""
    lines = content.splitlines()
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    subject = next((ln[len("Subject:") :].strip() for ln in lines[:80] if ln.startswith("Subject:")), "")

    for i, line in enumerate(lines, start=1):
        if line.startswith("diff --git "):
            parts = line.split()
            name = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else parts[-1]
            current = {"path": name, "hunks": [], "symbols": []}
            files.append(current)
        elif line.startswith("+++ "):
            name = line[4:].split("\t")[0].strip()
            if name != "/dev/null":
                name = name[2:] if name.startswith("b/") else name
                if current is None or (current["hunks"] and current["path"] != name):
                    current = {"path": name, "hunks": [], "symbols": []}
                    files.append(current)
                else:
                    current["path"] = name
        elif current is not None:
            m = HUNK_RE.match(line)
            if m:
                current["hunks"].append({"start": i, "end": i, "new": f"{m.group(3)},{m.group(4) or 1}", "ctx": m.group(5).strip()})
                continue
            if current["hunks"]:
                current["hunks"][-1]["end"] = i
            if line.startswith("+") and not line.startswith("+++"):
                for rx in SYMBOL_RES:
                    sm = rx.match(line)
                    if sm and sm.group(1) not in NOT_SYMBOLS:
                        if sm.group(1) not in current["symbols"]:
                            current["symbols"].append(sm.group(1))
                        break

    if not files:
        return extract_generic_chunks(file_path, rel_path, content)

    touched = [f["path"] for f in files]
    symbols = sorted({s for f in files for s in f["symbols"]})
    hunk_count = sum(len(f["hunks"]) for f in files)
    summary = [f"patch: {file_path.name}"]
    if subject:
        summary.append(f"subject: {subject}")
    summary.append(f"files touched ({len(touched)}): " + ", ".join(touched))
    summary.append(f"symbols added ({len(symbols)}): " + ", ".join(symbols))
    summary.append(f"hunks: {hunk_count}")
    chunks = [
        Chunk(rel_path, f"patch summary: {file_path.name}", "patch_summary", 1, min(len(lines), 30), "\n".join(summary))
    ]
    for f in files:
        for h in f["hunks"]:
            body = "\n".join(lines[h["start"] - 1 : h["end"]])[:PATCH_HUNK_CHARS]
            meta = f"file: {f['path']}\nnew range: {h['new']}\ncontext: {h['ctx']}\nsymbols added: {', '.join(f['symbols'])}\n"
            chunks.append(
                Chunk(rel_path, f"hunk {f['path']} @@ {h['ctx'] or h['new']}", "patch_hunk", h["start"], h["end"], meta + body)
            )
    return chunks


def chunk_file(file_path: Path, workspace_root: Path, rel_path: str | None = None) -> list[Chunk]:
    """Identifica o parser correto e divide o arquivo em blocos."""
    if rel_path is None:
        rel_path = str(file_path.relative_to(workspace_root))
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    suffix = file_path.suffix.lower()
    if suffix == ".py":
        return extract_python_chunks(file_path, rel_path, content)
    elif suffix in {".md", ".markdown"}:
        return extract_markdown_chunks(file_path, rel_path, content)
    elif suffix in {".patch", ".diff"}:
        return extract_patch_chunks(file_path, rel_path, content)
    elif suffix in {".c", ".h"}:
        from ctx.chunkers import extract_c_chunks

        c_chunks = extract_c_chunks(file_path, rel_path, content, Chunk)
        return c_chunks if c_chunks is not None else extract_generic_chunks(file_path, rel_path, content)
    elif suffix in {".dts", ".dtsi"}:
        from ctx.chunkers import extract_dts_chunks

        return extract_dts_chunks(file_path, rel_path, content, Chunk)
    else:
        return extract_generic_chunks(file_path, rel_path, content)


def is_binary_file(path: Path, sniff_bytes: int) -> bool:
    """Binário = byte NUL nos primeiros `sniff_bytes` bytes (ou arquivo ilegível)."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(sniff_bytes)
    except OSError:
        return True


def _parse_gitignore(root: Path) -> list[tuple[str, bool, bool]]:
    """Parser mínimo do .gitignore da raiz (fallback quando não há git): (padrão, só_diretório, ancorado)."""
    rules: list[tuple[str, bool, bool]] = []
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rules
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = line.startswith("/") or "/" in line
        rules.append((line.lstrip("/"), dir_only, anchored))
    return rules


def _gitignored(rel: str, is_dir: bool, rules: list[tuple[str, bool, bool]]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pattern, dir_only, anchored in rules:
        if dir_only and not is_dir:
            continue
        if anchored:
            if fnmatch.fnmatch(rel, pattern):
                return True
        elif fnmatch.fnmatch(name, pattern):
            return True
    return False


def _git_listed_files(workspace_root: Path) -> list[str] | None:
    """Arquivos rastreados + não rastreados não ignorados (respeita .gitignore). None se não houver git."""
    git = shutil.which("git")
    if not git or not (workspace_root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            [git, "-C", str(workspace_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _path_excluded(rel: str, idx_cfg: dict[str, Any]) -> bool:
    """Exclusões por diretório, extensão, glob e arquivos ocultos (mesma regra nos dois modos de coleta)."""
    parts = rel.split("/")
    excluded_dirs = set(idx_cfg["exclude_dirs"])
    excluded_dirs |= set(idx_cfg.get("extra_exclude_dirs", []))
    for d in parts[:-1]:
        if d in excluded_dirs or d.startswith(".") or d.endswith(".egg-info"):
            return True
    name = parts[-1]
    if name.startswith(".") and name not in {".gitignore", ".gitmodules"}:
        return True
    if Path(name).suffix.lower() in set(idx_cfg["exclude_extensions"]):
        return True
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g) for g in idx_cfg["exclude_globs"])


def _walk_files(workspace_root: Path, idx_cfg: dict[str, Any]) -> Iterator[str]:
    rules = _parse_gitignore(workspace_root) if idx_cfg["respect_gitignore"] else []
    for root, _dirs, files in os_walk_filtered(workspace_root, idx_cfg, rules):
        rel_dir = "" if root == workspace_root else root.relative_to(workspace_root).as_posix() + "/"
        for f in files:
            rel = rel_dir + f
            if rules and _gitignored(rel, False, rules):
                continue
            yield rel


def collect_files(
    workspace_root: Path, config: dict[str, Any] | None = None, policy: dict[str, Any] | None = None
) -> list[tuple[Path, str, str]]:
    """Arquivos a indexar: [(caminho, caminho_relativo, kind)] respeitando o escopo configurado.

    Exclui: ignorados pelo git, diretórios de build/cache/venv, extensões binárias, arquivos acima de
    `max_file_kb`, binários (byte NUL) e caminhos negados por `paths.deny`. A allowlist `[index.reference]`
    adiciona material fora do git (kind='reference').
    """
    config = config or load_config(workspace_root)
    policy = policy if policy is not None else load_raw_policy(workspace_root)
    idx = config["index"]
    max_bytes = int(idx["max_file_kb"]) * 1024
    sniff = int(idx["binary_sniff_bytes"])

    listed = _git_listed_files(workspace_root) if idx["respect_gitignore"] else None
    candidates = listed if listed is not None else list(_walk_files(workspace_root, idx))

    out: list[tuple[Path, str, str]] = []
    seen: set[str] = set()
    for rel in candidates:
        if rel in seen or _path_excluded(rel, idx):
            continue
        p = workspace_root / rel
        try:
            if not p.is_file() or p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        if is_denied(p, policy, workspace_root) or is_binary_file(p, sniff):
            continue
        seen.add(rel)
        out.append((p, rel, "code"))

    ref = idx.get("reference", {})
    ref_max = int(ref.get("max_file_kb", idx["max_file_kb"])) * 1024
    for pattern in ref.get("globs", []):
        expanded = os.path.expanduser(pattern)
        pat = expanded if os.path.isabs(expanded) else str(workspace_root / expanded)
        for match in sorted(globlib.glob(pat, recursive=True)):
            p = Path(match)
            if not p.is_file():
                continue
            try:
                rel = p.resolve().relative_to(workspace_root.resolve()).as_posix()
            except ValueError:
                rel = str(p.resolve())
            try:
                if rel in seen or p.stat().st_size > ref_max:
                    continue
            except OSError:
                continue
            if is_denied(p, policy, workspace_root) or is_binary_file(p, sniff):
                continue
            seen.add(rel)
            out.append((p, rel, "reference"))
    return out


def collect_indexable_files(workspace_root: Path) -> Iterator[Path]:
    """Compatibilidade: caminhos dos arquivos de código indexáveis (ver collect_files)."""
    for path, _rel, kind in collect_files(workspace_root):
        if kind == "code":
            yield path


def os_walk_filtered(
    root_dir: Path,
    idx_cfg: dict[str, Any] | None = None,
    gitignore_rules: list[tuple[str, bool, bool]] | None = None,
) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Percorre os diretórios podando pastas excluídas na descida (rápido, sem entrar em venv/build/.git)."""
    idx_cfg = idx_cfg or DEFAULTS["index"]
    excluded = set(idx_cfg["exclude_dirs"]) | set(idx_cfg.get("extra_exclude_dirs", []))
    rules = gitignore_rules or []
    stack = [root_dir]
    while stack:
        curr = stack.pop()
        try:
            entries = list(curr.iterdir())
        except PermissionError:
            continue

        dirs: list[str] = []
        files: list[str] = []
        for e in entries:
            name = e.name
            if e.is_dir():
                if name in excluded or name.startswith(".") or name.endswith(".egg-info"):
                    continue
                if rules and _gitignored((e.relative_to(root_dir)).as_posix(), True, rules):
                    continue
                dirs.append(name)
                stack.append(e)
            elif e.is_file():
                files.append(name)
        yield curr, dirs, files


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _delete_path(con: sqlite3.Connection, rel: str, tri: bool) -> None:
    if tri:
        con.execute("DELETE FROM chunks_tri WHERE rowid IN (SELECT rowid FROM chunks WHERE path = ?)", (rel,))
    con.execute("DELETE FROM chunks WHERE path = ?", (rel,))
    con.execute("DELETE FROM vectors WHERE path = ?", (rel,))


def file_name_terms(rel: str) -> str:
    """Nome do arquivo sem extensão, para a coluna `name` (M-26): `src/r36os/push.py` -> `push`."""
    return Path(rel).stem


def _store_file(
    con: sqlite3.Connection,
    path: Path,
    workspace_root: Path,
    rel: str,
    kind: str,
    st: os.stat_result,
    tri: bool = True,
    model: Any = None,
) -> None:
    chunks = chunk_file(path, workspace_root, rel_path=rel)
    _delete_path(con, rel, tri)
    name = file_name_terms(rel)
    rowids: list[int] = []
    for c in chunks:
        cur = con.execute(
            "INSERT INTO chunks (path, title, chunk_type, start_line, end_line, content, kind, name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (c.path, c.title, c.chunk_type, c.start_line, c.end_line, c.content, kind, name),
        )
        rowids.append(int(cur.lastrowid or 0))
    if tri and chunks:
        con.executemany(
            "INSERT INTO chunks_tri (rowid, path, title, content) VALUES (?, ?, ?, ?)",
            [(rid, c.path, c.title, c.content) for rid, c in zip(rowids, chunks)],
        )
    if model is not None and chunks:
        from ctx import semantic

        vecs = semantic.encode(model, [semantic.chunk_text(c.title, c.path, c.content) for c in chunks])
        con.executemany(
            "INSERT INTO vectors (id, path, vec) VALUES (?, ?, ?)",
            [(rid, rel, semantic.to_blob(v)) for rid, v in zip(rowids, vecs)],
        )
    con.execute(
        "INSERT OR REPLACE INTO indexed_files (path, mtime_ns, size, hash, kind) VALUES (?, ?, ?, ?, ?)",
        (rel, st.st_mtime_ns, st.st_size, "", kind),
    )


def index_workspace(
    workspace_root: Path = WORKSPACE_ROOT, force: bool = False, budget_ms: float | None = None, download: bool = True
) -> dict[str, Any]:
    """Indexa incrementalmente o repositório em SQLite FTS5 e consolida o WAL ao final.

    `budget_ms` limita o tempo de gravação (atualização incremental de busca): ao estourar, grava o que
    já processou e devolve `partial=True`. Arquivos do kind 'upstream' nunca são removidos aqui.
    """
    start_t = time.perf_counter()
    config = load_config(workspace_root)
    policy = load_raw_policy(workspace_root)
    db_path = db_path_for(workspace_root)
    con = get_db(db_path, config["index"])
    indexed_count = skipped_count = 0
    partial = False
    tri = has_table(con, "chunks_tri")
    model, notes = semantic_for_index(workspace_root, config, con, download)
    if model is not None and _meta(con, "semantic_model") != config["search"]["semantic_model"]:
        force = True  # modelo novo (ou extra recém-instalado): todos os trechos ganham vetores
    try:
        cached_meta = {
            row[0]: (row[1], row[2], row[3]) for row in con.execute("SELECT path, mtime_ns, size, kind FROM indexed_files")
        }
        current = collect_files(workspace_root, config, policy)
        seen: set[str] = set()
        deadline = start_t + budget_ms / 1000 if budget_ms else None

        with con:
            for path, rel, kind in current:
                seen.add(rel)
                try:
                    st = path.stat()
                except OSError:
                    continue
                cached = cached_meta.get(rel)
                if not force and cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and cached[2] == kind:
                    skipped_count += 1
                    continue
                if deadline and time.perf_counter() > deadline:
                    partial = True
                    continue
                _store_file(con, path, workspace_root, rel, kind, st, tri, model)
                indexed_count += 1

            if not partial:
                for cached_path, (_m, _s, cached_kind) in cached_meta.items():
                    if cached_path not in seen and cached_kind != "upstream":
                        _delete_path(con, cached_path, tri)
                        con.execute("DELETE FROM indexed_files WHERE path = ?", (cached_path,))
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('indexed_at', ?)", (str(time.time()),))
                con.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('semantic_model', ?)",
                    (config["search"]["semantic_model"] if model is not None else "",),
                )
    finally:
        con.close()
    # Consolida o WAL (journal_size_limit então o trunca): o índice nunca deve deixar um -wal gigante para trás.
    wal = checkpoint(db_path)
    elapsed_ms = (time.perf_counter() - start_t) * 1000

    return {
        "indexed": indexed_count,
        "skipped": skipped_count,
        "total": len(seen),
        "partial": partial,
        "wal_bytes": wal["after"],
        "elapsed_ms": round(elapsed_ms, 2),
        "notes": notes,
    }


def _meta(con: sqlite3.Connection, key: str) -> str:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row and row[0] is not None else ""


def semantic_for_index(
    workspace_root: Path, config: dict[str, Any], con: sqlite3.Connection, download: bool = True
) -> tuple[Any, list[str]]:
    """Modelo de embeddings para a indexação e avisos do que ficou desligado, cada um UMA vez.

    Só `ctx index` (download=True) baixa o modelo; a indexação disparada por uma busca usa o que já está no disco.
    """
    notes: list[str] = []

    def once(key: str, text: str) -> None:
        if _meta(con, key) != "1":
            notes.append(text)
            with con:
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", (key,))

    from ctx import chunkers, semantic

    if chunkers.c_parser() is None:
        once("warned_no_c", "C/H usam o corte genérico: instale o extra `c` (agent-harness-hub[c]) para cortar por função/struct.")
    if not config["search"].get("semantic", True):
        return None, notes
    if not semantic.available():
        once("warned_no_semantic", "busca semântica desligada: instale o extra `semantic` (agent-harness-hub[semantic]).")
        return None, notes
    if not download:
        return search_model(workspace_root, config), notes
    model = semantic.get_model(workspace_root, config["search"]["semantic_model"], allow_download=True)
    if model is None:
        notes.append(f"busca semântica: não foi possível carregar/baixar {config['search']['semantic_model']}; segue só o BM25.")
    return model, notes


def search_model(workspace_root: Path, config: dict[str, Any]) -> Any:
    """Modelo para a busca: só o que já está em .ctx/models (a busca nunca baixa nada nem acessa a rede)."""
    if not config["search"].get("semantic", True):
        return None
    from ctx import semantic

    if not semantic.model_dir(workspace_root, config["search"]["semantic_model"]).is_dir():
        return None
    return semantic.get_model(workspace_root, config["search"]["semantic_model"], allow_download=False)


def index_extra_file(workspace_root: Path, path: Path, kind: str = "upstream") -> str:
    """Indexa um arquivo fora do escopo normal (ex.: fonte upstream em .ctx/upstream). Retorna o caminho relativo."""
    try:
        rel = path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        rel = str(path.resolve())
    config = load_config(workspace_root)
    con = get_db(db_path_for(workspace_root), config["index"])
    model = search_model(workspace_root, config) if _meta(con, "semantic_model") else None
    try:
        with con:
            _store_file(con, path, workspace_root, rel, kind, path.stat(), has_table(con, "chunks_tri"), model)
    finally:
        con.close()
    checkpoint(db_path_for(workspace_root))
    return rel


def query_tokens(query: str) -> list[str]:
    """Palavras da consulta (sem os caracteres especiais do FTS5)."""
    tokens = re.findall(r"[\w\u00C0-\u017F]+", query)
    return tokens


# Palavras vazias (pt/en) retiradas da consulta lexical: numa pergunta em linguagem natural elas derrubam o
# "todos os termos" ("backlight do panel") sem ajudar a ranquear (M-11).
STOPWORDS = frozenset(
    """
    a o as os um uma uns umas de da do das dos em na no nas nos por para pra com sem que se ao aos
    e ou mas como onde quando qual quais quem porque fica ficar esta estao este esse isso ser foi
    the an of in on at to for from with without by and or but is are was be it its this that
    where when what which who how why does do
    """.split()
)


def fold(text: str) -> str:
    """Minúsculas sem acentos, para casar termos do glossário e palavras vazias."""
    import unicodedata

    return "".join(ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch))


def lexical_tokens(query: str) -> list[str]:
    """Termos da consulta sem palavras vazias (se só sobrarem palavras vazias, mantém todos)."""
    tokens = query_tokens(query)
    kept = [t for t in tokens if fold(t) not in STOPWORDS]
    return kept or tokens


def sanitize_query(query: str, mode: str = "and") -> str:
    """Higieniza os termos para o FTS5 sem quebras de sintaxe.

    "and": todos os termos (busca cirúrgica); "trigram": cada termo de 3+ caracteres como substring (tabela
    `chunks_tri`); "or": qualquer termo (último recuo, quando nenhum trecho tem todos).
    """
    tokens = lexical_tokens(query)
    if mode == "trigram":
        tokens = [t for t in tokens if len(t) >= 3]
    if not tokens:
        return ""
    quoted = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted) if mode == "or" else " ".join(quoted)


def expand_query(query: str, glossary: list[list[str]] | None) -> tuple[list[str], str, list[str]]:
    """Aplica o glossário do projeto (`[search].glossary`: grupos de termos equivalentes), M-11.

    Devolve (variantes para o BM25: a original e, se algo casou, a consulta com cada termo trocado pelo primeiro
    equivalente; texto expandido: original + todos os equivalentes, para o recuo "or" e a semântica; o que casou,
    para o cabeçalho do CLI).
    """
    if not glossary:
        return [query], query, []
    folded = fold(query)
    translated = folded
    extras: list[str] = []
    hits: list[str] = []
    for group in glossary:
        terms = [str(t) for t in (group if isinstance(group, list) else []) if str(t).strip()]
        for term in terms:
            rx = re.compile(rf"(?<!\w){re.escape(fold(term))}(?!\w)")
            if rx.search(folded):
                others = [t for t in terms if t != term]
                if others:
                    extras += others
                    translated = rx.sub(lambda _m, rep=others[0]: rep, translated)
                    hits.append(f"{term} → {', '.join(others)}")
                break
    variants = [query] + ([translated] if translated != folded else [])
    return variants, " ".join([query, *extras]), hits


def normalize_subpath(subpath: str) -> str:
    """Normaliza um subcaminho para prefixo relativo ('./src' -> 'src')."""
    sp = subpath.strip().removeprefix("./").rstrip("/")
    return "" if sp in ("", ".") else sp


def path_matches(path: str, subpaths: list[str]) -> bool:
    """True se `path` é o próprio subcaminho ou está dentro dele (lista vazia casa tudo)."""
    prefixes = [normalize_subpath(s) for s in subpaths]
    if not prefixes or "" in prefixes:
        return True
    return any(path == p or path.startswith(p + "/") for p in prefixes)


PREVIEW_CONTEXT = 3  # linhas antes da primeira ocorrência quando a prévia é centrada no termo (M-06)
PREVIEW_LINE_CHARS = 200  # corte por linha da prévia: JSONL/minificados não inundam a saída


def cut_line(text: str) -> str:
    return text if len(text) <= PREVIEW_LINE_CHARS else text[:PREVIEW_LINE_CHARS] + f" …(+{len(text) - PREVIEW_LINE_CHARS})"


def read_preview(
    workspace_root: Path,
    rel_path: str,
    start: int,
    end: int,
    max_lines: int,
    cache: dict[str, list[str]],
    terms: list[str] | None = None,
) -> str:
    """Lê até `max_lines` linhas reais do arquivo, sem passar de `end`.

    Com `terms` e trecho maior que a prévia, ela começa `PREVIEW_CONTEXT` linhas antes da primeira linha que contém
    um dos termos (sem diferenciar maiúsculas), e não no início do trecho (que pode ser cabeçalho de licença).
    """
    if max_lines <= 0:
        return ""
    if rel_path not in cache:
        try:
            cache[rel_path] = (workspace_root / rel_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[rel_path] = []
    lines = cache[rel_path]
    start, end = int(start), int(end)
    if terms and end - start + 1 > max_lines:
        low = [t.lower() for t in terms]
        for n in range(start, min(end, len(lines)) + 1):
            text = lines[n - 1].lower()
            if any(t in text for t in low):
                start = max(start, min(n - PREVIEW_CONTEXT, end - max_lines + 1))
                break
    stop = min(end, start + max_lines - 1, len(lines))
    width = len(str(stop))
    return "\n".join(f"{n:>{width}}  {cut_line(lines[n - 1])}" for n in range(start, stop + 1))


def make_snippet(content: str, terms: list[str], width: int = 120) -> str:
    """Trecho de uma linha em volta da primeira ocorrência de um termo, com o termo marcado (>>> <<<)."""
    low = content.lower()
    pos, term = min(((low.find(t.lower()), t) for t in terms if t and t.lower() in low), default=(-1, ""))
    if pos < 0:
        return " ".join(content[:width].split())
    a = max(0, pos - width // 2)
    text = content[a : pos] + ">>> " + content[pos : pos + len(term)] + " <<<" + content[pos + len(term) : a + width]
    return ("..." if a else "") + " ".join(text.split()) + "..."


def rg_equivalent(query: str, path_prefix: str | None = None) -> str:
    """Comando `rg` aproximadamente equivalente à query (fallback quando a busca estoura o tempo)."""
    parts = ["rg", "-n", "--heading", "-C", "2"]
    for term in re.findall(r"[\wÀ-ſ]+", query):
        parts += ["-e", term]
    prefix = normalize_subpath(path_prefix or "")
    if prefix:
        parts.append(prefix)
    return " ".join(shlex.quote(p) for p in parts)


def _kinds_for_scope(scope: str) -> list[str] | None:
    if scope in ("", "all", None):
        return None
    if scope not in KINDS:
        raise ValueError(f"scope inválido '{scope}' (use: all, {', '.join(KINDS)})")
    return [scope]


def repair_bloat(workspace_root: Path, config: dict[str, Any]) -> list[str]:
    """WAL inchado: consolida (checkpoint) ou, se gigante, descarta o índice (cache) para recriá-lo. Retorna avisos.

    Roda ANTES de abrir o banco: abrir um índice com dezenas de GB de WAL já seria o travamento.
    """
    db = db_path_for(workspace_root)
    wal = wal_bytes(db)
    if wal > WAL_REBUILD_BYTES:
        remove_db(db)
        return [f"WAL de {wal // (1024 * 1024)} MB descartado: índice recriado (rode `ctx doctor`)"]
    if wal > int(config["doctor"]["wal_max_mb"]) * 1024 * 1024:
        checkpoint(db)
        return [f"WAL de {wal // (1024 * 1024)} MB consolidado"]
    return []


def ensure_current(workspace_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Prepara o índice para leitura SEM escrever conteúdo na busca comum.

    Só escreve quando é indispensável: índice ausente/de versão anterior/corrompido (reindexa), WAL inchado
    (consolida ou recria) ou, se `[search].refresh = "incremental"`, uma atualização com orçamento de tempo.
    """
    db = db_path_for(workspace_root)
    info: dict[str, Any] = {"reindexed": False, "repaired": False, "warnings": []}
    info["warnings"] += repair_bloat(workspace_root, config)
    info["repaired"] = bool(info["warnings"])
    state = index_state(db)
    if state != "ok":
        if state == "broken":
            remove_db(db)
        res = index_workspace(workspace_root, download=False)
        info["reindexed"] = True
        info["warnings"].append(
            f"índice {'criado' if state == 'missing' else 'recriado (versão anterior/corrompido)'}: {res['total']} arquivos em {res['elapsed_ms']}ms"
        )
    elif config["search"]["refresh"] == "incremental":
        res = index_workspace(workspace_root, budget_ms=float(config["search"]["refresh_budget_ms"]), download=False)
        info["reindexed"] = res["indexed"] > 0
        if res["partial"]:
            info["warnings"].append("atualização incremental parcial (orçamento de tempo); rode `ctx index`")
    return info


def reindex_paths(workspace_root: Path, paths: list[str], config: dict[str, Any], budget_ms: float) -> int:
    """Reindexa só estes arquivos (mudaram desde a indexação), dentro do orçamento. Retorna quantos foram tratados."""
    start = time.perf_counter()
    db = db_path_for(workspace_root)
    con = get_db(db, config["index"])
    tri = has_table(con, "chunks_tri")
    done = 0
    try:
        with con:
            for rel in paths:
                if (time.perf_counter() - start) * 1000 > budget_ms:
                    break
                row = con.execute("SELECT kind FROM indexed_files WHERE path = ?", (rel,)).fetchone()
                kind = row[0] if row else "code"
                path = Path(rel) if Path(rel).is_absolute() else workspace_root / rel
                try:
                    st = path.stat()
                except OSError:
                    _delete_path(con, rel, tri)
                    con.execute("DELETE FROM indexed_files WHERE path = ?", (rel,))
                    done += 1
                    continue
                _store_file(con, path, workspace_root, rel, kind, st, tri, search_model(workspace_root, config))
                done += 1
    finally:
        con.close()
    checkpoint(db)
    return done


def _stale_results(con: sqlite3.Connection, workspace_root: Path, paths: set[str]) -> list[str]:
    """Arquivos presentes nos resultados que mudaram desde a indexação (checagem barata: só o que foi retornado)."""
    stale: list[str] = []
    for path in sorted(paths):
        row = con.execute("SELECT mtime_ns, size FROM indexed_files WHERE path = ?", (path,)).fetchone()
        try:
            st = (workspace_root / path).stat()
        except OSError:
            stale.append(path)
            continue
        if row and (row[0] != st.st_mtime_ns or row[1] != st.st_size):
            stale.append(path)
    return stale


def _search_one(
    con: sqlite3.Connection,
    workspace_root: Path,
    query: str,
    top_k: int,
    path_prefix: str | None,
    lines: int,
    file_cache: dict[str, list[str]],
    kinds: list[str] | None = None,
    deadline: float | None = None,
    glossary: list[list[str]] | None = None,
    model: Any = None,
) -> dict[str, Any]:
    """BM25 ponderado com recuo (todos os termos -> substring -> qualquer termo), fundido por RRF com as outras
    listas de candidatos (semântica, quando instalada). `mode` diz qual passo do recuo trouxe o resultado."""
    start_t = time.perf_counter()
    prefix = normalize_subpath(path_prefix or "")
    results: list[dict[str, Any]] = []
    timed_out = False
    mode = ""
    variants, expanded, glossary_hits = expand_query(query, glossary)

    if deadline is not None and start_t >= deadline:
        timed_out = True
    elif query_tokens(query):
        pool = max(HYBRID_POOL, top_k)
        try:
            lexical, mode = _lexical_rowids(con, query, prefix, kinds, pool, variants, expanded)
        except sqlite3.OperationalError:
            # "interrupted" = o progress handler estourou o prazo
            lexical, mode = [], ""
            timed_out = deadline is not None and time.perf_counter() >= deadline
        sem: list[int] = []
        # a semântica só entra quando o BM25 não resolveu sozinho (todos os termos em top_k trechos)
        if model is not None and not timed_out and not (mode.startswith("and") and len(lexical) >= top_k):
            sem = _semantic_rowids(con, model, expanded, prefix, kinds, pool)
            if sem and not lexical:
                mode = "semantic"
        terms = lexical_tokens(expanded)
        candidates: list[tuple[float, str, tuple[Any, ...]]] = []
        for rid, score, via in fuse([(1.0, "bm25", lexical), (SEMANTIC_WEIGHT, "sem", sem)]):
            row = con.execute(
                "SELECT path, title, chunk_type, start_line, end_line, kind, content FROM chunks WHERE rowid = ?", (rid,)
            ).fetchone()
            if row is not None:
                candidates.append((score * name_bonus(row[0], terms), via, row))
        candidates.sort(key=lambda c: -c[0])
        for score, via, row in candidates[:top_k]:
            if deadline is not None and time.perf_counter() > deadline:
                timed_out = True
                break
            results.append(
                {
                    "path": row[0],
                    "title": row[1],
                    "chunk_type": row[2],
                    "slice": f"{row[0]}:{row[3]}-{row[4]}",
                    "start_line": row[3],
                    "end_line": row[4],
                    "snippet": make_snippet(row[6], terms),
                    "preview": read_preview(workspace_root, row[0], row[3], row[4], lines, file_cache, terms),
                    "score": round(score * 1000, 2),
                    "kind": row[5],
                    "via": via,
                }
            )

    out = {
        "query": query,
        "results": results,
        "count": len(results),
        "mode": mode,
        "glossary": glossary_hits,
        "timed_out": timed_out,
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }
    if timed_out:
        out["rg"] = rg_equivalent(query, path_prefix)
    return out


def _filters(prefix: str, kinds: list[str] | None, col: str = "") -> tuple[str, list[Any]]:
    """Cláusulas extras (subcaminho e kind) sobre `chunks` (`col` = prefixo da tabela na consulta)."""
    sql, params = "", []
    if prefix:
        sql += f" AND ({col}path = ? OR {col}path LIKE ? ESCAPE '\\')"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [prefix, escaped + "/%"]
    if kinds:
        sql += f" AND {col}kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    return sql, params


def _lexical_rowids(
    con: sqlite3.Connection,
    query: str,
    prefix: str,
    kinds: list[str] | None,
    limit: int,
    variants: list[str] | None = None,
    expanded: str | None = None,
) -> tuple[list[int], str]:
    """rowids por BM25 com recuo (M-01): todos os termos -> substrings (trigramas, M-04) -> qualquer termo.

    Em cada modo tenta a consulta original e depois a traduzida pelo glossário; o "or" usa o texto expandido.
    Devolve (rowids em ordem, modo usado; "+glossary" quando foi a variante do glossário).
    """
    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    variants = variants or [query]
    tri = has_table(con, "chunks_tri")
    attempts = [(m, v) for m in ("and", "trigram") for v in variants] + [("or", expanded or query)]
    for mode, text in attempts:
        match = sanitize_query(text, mode)
        if not match or (mode == "trigram" and not tri) or (mode == "or" and len(lexical_tokens(text)) < 2):
            continue
        if mode == "trigram":
            extra, params = _filters(prefix, kinds, "c.")
            sql = (
                "SELECT t.rowid FROM chunks_tri AS t JOIN chunks AS c ON c.rowid = t.rowid "
                f"WHERE chunks_tri MATCH ?{extra} ORDER BY bm25(chunks_tri) LIMIT ?"
            )
        else:
            extra, params = _filters(prefix, kinds)
            sql = f"SELECT rowid FROM chunks WHERE chunks MATCH ?{extra} ORDER BY bm25(chunks, {weights}) LIMIT ?"
        try:
            rows = [int(r[0]) for r in con.execute(sql, [match, *params, limit]).fetchall()]
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e):
                raise
            rows = []
        if rows:
            return rows, mode + ("+glossary" if text != query else "")
    return [], ""


SEMANTIC_WEIGHT = 0.8  # peso da lista semântica na fusão (BM25 = 1.0): o termo exato vence o empate


def _semantic_rowids(
    con: sqlite3.Connection, model: Any, query: str, prefix: str, kinds: list[str] | None, limit: int
) -> list[int]:
    from ctx import semantic

    try:
        cands = semantic.top_rowids(con, model, query, limit * 4)
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e):
            raise
        return []
    except Exception:  # noqa: BLE001 - semântica é opcional
        return []
    if not cands:
        return []
    ids = [rid for rid, _ in cands]
    extra, params = _filters(prefix, kinds)
    marks = ",".join("?" * len(ids))
    allowed = {int(r[0]) for r in con.execute(f"SELECT rowid FROM chunks WHERE rowid IN ({marks}){extra}", [*ids, *params])}
    return [rid for rid in ids if rid in allowed][:limit]


def fuse(lists: list[tuple[float, str, list[int]]]) -> list[tuple[int, float, str]]:
    """Reciprocal rank fusion (k = RRF_K): (rowid, pontuação, origem `bm25`/`sem`/`bm25+sem`), melhor primeiro."""
    scores: dict[int, float] = {}
    via: dict[int, set[str]] = {}
    for weight, label, ids in lists:
        for rank, rid in enumerate(ids, start=1):
            scores[rid] = scores.get(rid, 0.0) + weight / (RRF_K + rank)
            via.setdefault(rid, set()).add(label)
    return [(rid, scores[rid], "+".join(sorted(via[rid]))) for rid in sorted(scores, key=lambda r: -scores[r])]


def search_many(
    queries: list[str],
    top_k: int = 5,
    workspace_root: Path = WORKSPACE_ROOT,
    path_prefix: str | None = None,
    lines: int = DEFAULT_PREVIEW_LINES,
    timeout: float | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """Busca multi-query One-Shot rankeada por BM25, com preview de `lines` linhas por resultado.

    Somente leitura (exceto reparo/primeira indexação, ver ensure_current). Nenhuma busca passa de `timeout`
    segundos (padrão `[search].timeout_s`): ao estourar, devolve o que tem e o comando `rg` equivalente.
    """
    start_t = time.perf_counter()
    config = load_config(workspace_root)
    timeout = float(config["search"]["timeout_s"] if timeout is None else timeout)
    kinds = _kinds_for_scope(scope)
    info = ensure_current(workspace_root, config)
    # O prazo vale para a busca em si; a (rara) indexação inicial/reparo de ensure_current não consome o orçamento.
    deadline = time.perf_counter() + timeout

    glossary = config["search"].get("glossary") or []
    model = search_model(workspace_root, config)  # nunca baixa nada na busca
    warnings = list(info["warnings"])

    def run() -> tuple[list[dict[str, Any]], list[str]]:
        con = open_readonly(db_path_for(workspace_root))
        file_cache: dict[str, list[str]] = {}
        con.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10000)
        try:
            found_q = [
                _search_one(con, workspace_root, q, top_k, path_prefix, lines, file_cache, kinds, deadline, glossary, model)
                for q in queries
            ]
            con.set_progress_handler(None, 0)
            return found_q, _stale_results(con, workspace_root, {r["path"] for q in found_q for r in q["results"]})
        finally:
            con.close()

    per_query, stale = run()
    if stale and config["search"]["refresh"] == "incremental":
        # M-26: os poucos arquivos dos resultados que mudaram são reindexados na hora e a busca é refeita
        done = reindex_paths(workspace_root, stale, config, float(config["search"]["refresh_budget_ms"]))
        if done:
            per_query, stale = run()
    if stale:
        warnings.append(
            f"{len(stale)} arquivo(s) nos resultados mudaram desde a indexação ({', '.join(stale[:3])}"
            f"{'…' if len(stale) > 3 else ''}); rode `ctx index` para atualizar"
        )
    timed_out = any(q["timed_out"] for q in per_query)
    if timed_out:
        warnings.append(f"busca interrompida após {timeout:g}s; use o comando rg sugerido")
    return {
        "queries": per_query,
        "path": path_prefix,
        "scope": scope,
        "timed_out": timed_out,
        "warnings": warnings,
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }


def search(
    query: str,
    top_k: int = 5,
    workspace_root: Path = WORKSPACE_ROOT,
    path_prefix: str | None = None,
    lines: int = DEFAULT_PREVIEW_LINES,
    timeout: float | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """Busca cirúrgica de uma única query (atalho para search_many)."""
    res = search_many(
        [query], top_k=top_k, workspace_root=workspace_root, path_prefix=path_prefix, lines=lines, timeout=timeout, scope=scope
    )
    single = res["queries"][0]
    single["elapsed_ms"] = res["elapsed_ms"]
    single["warnings"] = res["warnings"]
    return single


def map_priority(path: str) -> tuple[int, str]:
    """Ordena código ativo (src/, scripts/, tests/, changes ativas) antes de config e documentação."""
    for i, prefix in enumerate(MAP_PRIORITY):
        if path.startswith(prefix):
            return i, path
    if path.endswith((".md", ".markdown")):
        return len(MAP_PRIORITY) + 1, path
    return len(MAP_PRIORITY), path


def get_map(
    workspace_root: Path = WORKSPACE_ROOT,
    subpaths: list[str] | None = None,
    include_archive: bool = False,
) -> dict[str, Any]:
    """Mapa estrutural multi-alvo (oculta openspec/changes/archive/ por padrão)."""
    start_t = time.perf_counter()
    ensure_current(workspace_root, load_config(workspace_root))
    con = open_readonly(db_path_for(workspace_root))
    try:
        rows = con.execute("SELECT path, title, chunk_type, start_line, end_line FROM chunks WHERE kind = 'code'").fetchall()
    finally:
        con.close()

    subpaths = subpaths or []
    grouped: dict[str, list[dict[str, Any]]] = {}
    hidden_archive = set()
    for path, title, ctype, start, end in rows:
        if not path_matches(path, subpaths):
            continue
        if not include_archive and path.startswith(ARCHIVE_PREFIX):
            hidden_archive.add(path)
            continue
        grouped.setdefault(path, []).append(
            {"title": title, "type": ctype, "lines": f"L{start}-L{end}", "_start": int(start)}
        )

    tree: dict[str, list[dict[str, Any]]] = {}
    total_chunks = 0
    for path in sorted(grouped, key=map_priority):
        chunks = sorted(grouped[path], key=lambda c: c["_start"])
        for c in chunks:
            c.pop("_start")
        tree[path] = chunks
        total_chunks += len(chunks)

    return {
        "files_count": len(tree),
        "chunks_count": total_chunks,
        "tree": tree,
        "subpaths": subpaths,
        "hidden_archive_files": len(hidden_archive),
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }


TASK_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")


def parse_tasks(tasks_file: Path, max_pending: int = 3) -> dict[str, Any]:
    """Conta checkboxes de um tasks.md (OpenSpec) e retorna as próximas tarefas pendentes."""
    done = total = 0
    pending: list[str] = []
    try:
        text = tasks_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"done": 0, "total": 0, "next": []}
    for line in text.splitlines():
        m = TASK_RE.match(line)
        if not m:
            continue
        total += 1
        if m.group(1).lower() == "x":
            done += 1
        elif len(pending) < max_pending:
            pending.append(m.group(2).strip())
    return {"done": done, "total": total, "next": pending}


def openspec_status(workspace_root: Path) -> dict[str, Any]:
    """Changes OpenSpec ativas (com progresso X/Y) separadas das arquivadas."""
    changes_dir = workspace_root / "openspec" / "changes"
    if not changes_dir.is_dir():
        return {"present": False, "active": [], "archived_count": 0}
    active = []
    for d in sorted(p for p in changes_dir.iterdir() if p.is_dir() and p.name != "archive"):
        info = parse_tasks(d / "tasks.md")
        active.append({"name": d.name, **info})
    archive_dir = changes_dir / "archive"
    archived = sum(1 for p in archive_dir.iterdir() if p.is_dir()) if archive_dir.is_dir() else 0
    return {"present": True, "active": active, "archived_count": archived}


def git_status(workspace_root: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if not git or not (workspace_root / ".git").exists():
        return {"available": False, "lines": []}
    try:
        out = subprocess.run(
            [git, "-C", str(workspace_root), "status", "-s", "-b"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "lines": []}
    return {"available": out.returncode == 0, "lines": out.stdout.splitlines()}


def scope_summary(config: dict[str, Any]) -> str:
    idx = config["index"]
    parts = ["gitignore" if idx["respect_gitignore"] else "sem gitignore", f"≤{idx['max_file_kb']}KB", "sem binários"]
    if idx["reference"]["globs"]:
        parts.append(f"referência: {', '.join(idx['reference']['globs'])}")
    return ", ".join(parts)


def index_info(workspace_root: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tamanho, contagem, idade e escopo do índice (somente leitura)."""
    config = config or load_config(workspace_root)
    db = db_path_for(workspace_root)
    info: dict[str, Any] = {
        "present": db.exists(),
        "files": 0,
        "chunks": 0,
        "by_kind": {},
        "db_bytes": db.stat().st_size if db.exists() else 0,
        "wal_bytes": wal_bytes(db),
        "indexed_at": None,
        "age_s": None,
        "scope": scope_summary(config),
    }
    if not db.exists():
        return info
    try:
        con = open_readonly(db)
        try:
            info["files"] = con.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
            info["chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            info["by_kind"] = dict(con.execute("SELECT kind, COUNT(*) FROM indexed_files GROUP BY kind").fetchall())
            row = con.execute("SELECT value FROM meta WHERE key = 'indexed_at'").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return info
    if row:
        info["indexed_at"] = float(row[0])
        info["age_s"] = max(0, int(time.time() - float(row[0])))
    return info


def pending_changes(workspace_root: Path) -> int:
    """Quantos arquivos do escopo diferem do índice (novos, alterados ou removidos). Checagem completa (doctor)."""
    config = load_config(workspace_root)
    db = db_path_for(workspace_root)
    if index_state(db) != "ok":
        return -1
    con = open_readonly(db)
    try:
        cached = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime_ns, size FROM indexed_files WHERE kind != 'upstream'")}
    finally:
        con.close()
    pending = 0
    seen: set[str] = set()
    for path, rel, _kind in collect_files(workspace_root, config):
        seen.add(rel)
        try:
            st = path.stat()
        except OSError:
            continue
        if cached.get(rel) != (st.st_mtime_ns, st.st_size):
            pending += 1
    return pending + sum(1 for p in cached if p not in seen)


def status(workspace_root: Path = WORKSPACE_ROOT) -> dict[str, Any]:
    """Visão consolidada One-Shot: Git, OpenSpec, saúde do índice FTS5 e do cache de binários."""
    start_t = time.perf_counter()
    config = load_config(workspace_root)
    ensure = ensure_current(workspace_root, config)
    info = index_info(workspace_root, config)

    bins_file = workspace_root / ".ctx" / "bins.json"
    bins: dict[str, Any] = {"present": bins_file.exists()}
    if bins_file.exists():
        try:
            data = json.loads(bins_file.read_text(encoding="utf-8"))
            binaries = data.get("binaries", {})
            bins.update(
                {
                    "generated_at": data.get("generated_at"),
                    "available": sum(1 for v in binaries.values() if v.get("available")),
                    "total": len(binaries),
                }
            )
        except (OSError, ValueError):
            bins["present"] = False

    return {
        "git": git_status(workspace_root),
        "openspec": openspec_status(workspace_root),
        "index": {**info, "reindexed": 1 if ensure["reindexed"] else 0, "warnings": ensure["warnings"]},
        "bins": bins,
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }
