"""Motor de RAG local One-Shot baseado em SQLite FTS5/BM25 e AST/Markdown Chunking.

Inclui:
- Busca multi-query com preview de N linhas e filtro por subcaminho (`ctx search`).
- Mapa estrutural multi-alvo que oculta `openspec/changes/archive/` (`ctx map`).
- Visão consolidada One-Shot: Git + OpenSpec + saúde do índice/cache (`ctx status`).

Zero dependências externas (100% biblioteca padrão: sqlite3, ast, json, pathlib, subprocess, time).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ctx import WORKSPACE_ROOT

ARCHIVE_PREFIX = "openspec/changes/archive/"
MAP_PRIORITY = ("src/", "scripts/", "tests/", "openspec/changes/")
DEFAULT_PREVIEW_LINES = 25

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


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Abre conexão com o banco SQLite em modo WAL e cria as tabelas necessárias."""
    db_path = db_path or db_path_for(WORKSPACE_ROOT)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS indexed_files (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER,
            size INTEGER,
            hash TEXT
        );
        """
    )

    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            path,
            title,
            chunk_type,
            start_line UNINDEXED,
            end_line UNINDEXED,
            content,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    return con


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
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    chunks.append(
                        Chunk(
                            path=rel_path,
                            title=current_title,
                            chunk_type="markdown_section",
                            start_line=current_start,
                            end_line=i - 1,
                            content=chunk_text,
                        )
                    )
            current_title = match.group(2).strip()
            current_start = i
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if chunk_text:
            chunks.append(
                Chunk(
                    path=rel_path,
                    title=current_title,
                    chunk_type="markdown_section",
                    start_line=current_start,
                    end_line=len(lines),
                    content=chunk_text,
                )
            )

    return chunks


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


def chunk_file(file_path: Path, workspace_root: Path) -> list[Chunk]:
    """Identifica o parser correto e divide o arquivo em blocos."""
    rel_path = str(file_path.relative_to(workspace_root))
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if file_path.suffix == ".py":
        return extract_python_chunks(file_path, rel_path, content)
    elif file_path.suffix in {".md", ".markdown"}:
        return extract_markdown_chunks(file_path, rel_path, content)
    else:
        return extract_generic_chunks(file_path, rel_path, content)


def collect_indexable_files(workspace_root: Path) -> Iterator[Path]:
    """Varre o repositório ignorando diretórios e extensões irrelevantes."""
    for root, dirs, files in os_walk_filtered(workspace_root):
        for f in files:
            p = root / f
            if p.suffix.lower() in IGNORED_EXTENSIONS:
                continue
            if f.startswith(".") and f not in {".gitignore", ".gitmodules"}:
                continue
            yield p


def os_walk_filtered(root_dir: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Percorre os diretórios filtrando pastas ignoradas na descida para alta velocidade."""
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
                if name not in IGNORED_DIRS and not name.startswith(".") and not name.endswith(".egg-info"):
                    dirs.append(name)
                    stack.append(e)
            elif e.is_file():
                files.append(name)
        yield curr, dirs, files


def index_workspace(workspace_root: Path = WORKSPACE_ROOT, force: bool = False) -> dict[str, Any]:
    """Indexa incrementalmente o repositório em SQLite FTS5."""
    start_t = time.perf_counter()
    con = get_db(db_path_for(workspace_root))

    existing_rows = con.execute("SELECT path, mtime_ns, size FROM indexed_files").fetchall()
    cached_meta = {row[0]: (row[1], row[2]) for row in existing_rows}

    current_files = list(collect_indexable_files(workspace_root))
    seen_rel_paths: set[str] = set()

    indexed_count = 0
    skipped_count = 0

    with con:
        for file_path in current_files:
            rel_path = str(file_path.relative_to(workspace_root))
            seen_rel_paths.add(rel_path)

            stat = file_path.stat()
            mtime_ns = stat.st_mtime_ns
            size = stat.st_size

            if not force and rel_path in cached_meta:
                cached_mtime, cached_size = cached_meta[rel_path]
                if cached_mtime == mtime_ns and cached_size == size:
                    skipped_count += 1
                    continue

            chunks = chunk_file(file_path, workspace_root)
            con.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
            for c in chunks:
                con.execute(
                    """
                    INSERT INTO chunks (path, title, chunk_type, start_line, end_line, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (c.path, c.title, c.chunk_type, c.start_line, c.end_line, c.content),
                )

            con.execute(
                """
                INSERT OR REPLACE INTO indexed_files (path, mtime_ns, size, hash)
                VALUES (?, ?, ?, ?)
                """,
                (rel_path, mtime_ns, size, ""),
            )
            indexed_count += 1

        # Remover arquivos deletados
        for cached_path in cached_meta:
            if cached_path not in seen_rel_paths:
                con.execute("DELETE FROM chunks WHERE path = ?", (cached_path,))
                con.execute("DELETE FROM indexed_files WHERE path = ?", (cached_path,))

    con.close()
    elapsed_ms = (time.perf_counter() - start_t) * 1000

    return {
        "indexed": indexed_count,
        "skipped": skipped_count,
        "total": len(seen_rel_paths),
        "elapsed_ms": round(elapsed_ms, 2),
    }


def sanitize_query(query: str) -> str:
    """Higieniza termos para consulta estrita no FTS5 sem quebras de sintaxe."""
    # Remove caracteres especiais do FTS5 preservando palavras
    tokens = re.findall(r"[\w\u00C0-\u017F]+", query)
    if not tokens:
        return ""
    # Junta com AND implícito para busca cirúrgica
    return " ".join(f'"{t}"' for t in tokens)


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


def read_preview(workspace_root: Path, rel_path: str, start: int, end: int, max_lines: int, cache: dict[str, list[str]]) -> str:
    """Lê até `max_lines` linhas reais do arquivo a partir de `start` (sem passar de `end`)."""
    if max_lines <= 0:
        return ""
    if rel_path not in cache:
        try:
            cache[rel_path] = (workspace_root / rel_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[rel_path] = []
    lines = cache[rel_path]
    stop = min(int(end), int(start) + max_lines - 1, len(lines))
    width = len(str(stop))
    return "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(int(start), stop + 1))


def _search_one(
    con: sqlite3.Connection,
    workspace_root: Path,
    query: str,
    top_k: int,
    path_prefix: str | None,
    lines: int,
    file_cache: dict[str, list[str]],
) -> dict[str, Any]:
    start_t = time.perf_counter()
    sanitized = sanitize_query(query)
    results: list[dict[str, Any]] = []

    if sanitized:
        sql = """
            SELECT path, title, chunk_type, start_line, end_line,
                   snippet(chunks, 5, '>>> ', ' <<<', '...', 25) AS snip,
                   bm25(chunks) AS rank
            FROM chunks
            WHERE chunks MATCH ?
        """
        params: list[Any] = [sanitized]
        prefix = normalize_subpath(path_prefix or "")
        if prefix:
            sql += " AND (path = ? OR path LIKE ? ESCAPE '\\')"
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params += [prefix, escaped + "/%"]
        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)
        try:
            for row in con.execute(sql, params).fetchall():
                results.append(
                    {
                        "path": row[0],
                        "title": row[1],
                        "chunk_type": row[2],
                        "slice": f"{row[0]}:{row[3]}-{row[4]}",
                        "start_line": row[3],
                        "end_line": row[4],
                        "snippet": row[5].strip(),
                        "preview": read_preview(workspace_root, row[0], row[3], row[4], lines, file_cache),
                        "score": round(float(row[6]), 3),
                    }
                )
        except sqlite3.OperationalError:
            pass

    return {
        "query": query,
        "results": results,
        "count": len(results),
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }


def search_many(
    queries: list[str],
    top_k: int = 5,
    workspace_root: Path = WORKSPACE_ROOT,
    path_prefix: str | None = None,
    lines: int = DEFAULT_PREVIEW_LINES,
) -> dict[str, Any]:
    """Busca multi-query One-Shot rankeada por BM25, com preview de `lines` linhas por resultado."""
    index_workspace(workspace_root=workspace_root, force=False)
    start_t = time.perf_counter()
    con = get_db(db_path_for(workspace_root))
    file_cache: dict[str, list[str]] = {}
    try:
        per_query = [_search_one(con, workspace_root, q, top_k, path_prefix, lines, file_cache) for q in queries]
    finally:
        con.close()
    return {
        "queries": per_query,
        "path": path_prefix,
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }


def search(
    query: str,
    top_k: int = 5,
    workspace_root: Path = WORKSPACE_ROOT,
    path_prefix: str | None = None,
    lines: int = DEFAULT_PREVIEW_LINES,
) -> dict[str, Any]:
    """Busca cirúrgica de uma única query (atalho para search_many)."""
    res = search_many([query], top_k=top_k, workspace_root=workspace_root, path_prefix=path_prefix, lines=lines)
    single = res["queries"][0]
    single["elapsed_ms"] = res["elapsed_ms"]
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
    index_workspace(workspace_root=workspace_root, force=False)
    start_t = time.perf_counter()
    con = get_db(db_path_for(workspace_root))
    try:
        rows = con.execute("SELECT path, title, chunk_type, start_line, end_line FROM chunks").fetchall()
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


def status(workspace_root: Path = WORKSPACE_ROOT) -> dict[str, Any]:
    """Visão consolidada One-Shot: Git, OpenSpec, saúde do índice FTS5 e do cache de binários."""
    start_t = time.perf_counter()
    idx = index_workspace(workspace_root=workspace_root, force=False)

    con = get_db(db_path_for(workspace_root))
    try:
        files_count = con.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
        chunks_count = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        con.close()

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
        "index": {"files": files_count, "chunks": chunks_count, "reindexed": idx["indexed"]},
        "bins": bins,
        "elapsed_ms": round((time.perf_counter() - start_t) * 1000, 2),
    }
