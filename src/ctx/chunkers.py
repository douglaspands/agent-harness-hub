"""Cortes estruturais para C e Device Tree (M-03).

- C/H: tree-sitter-c (extra opcional `c`). Cada definição de nível superior vira um trecho com o nome do símbolo no
  título (função, struct/enum/union, typedef, tabela estática, macro de 3+ linhas), com o comentário logo acima.
  Definições acima de MAX_CHUNK_LINES viram partes com o mesmo título. O que fica entre definições usa janelas de
  GAP_STEP linhas, com os `#define` listados no título. Sem tree-sitter, `extract_c_chunks` devolve None e o
  chamador usa o corte genérico.
- DTS/DTSI: sem dependência, por contagem de chaves. Cada nó de profundidade 1 dentro de `/ { }` e cada
  `&rótulo { }` de nível superior vira um trecho com rótulo e nome no título (`backlight: backlight`, `&dsi`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

MAX_CHUNK_LINES = 120  # funções/nós maiores são divididos em partes
GAP_STEP = 50  # trechos entre definições (includes, comentários) usam janelas, como o corte genérico

_C_PARSER: Any = None


def c_parser() -> Any:
    """Parser de C do tree-sitter, ou None se a dependência opcional não estiver instalada (cache por processo)."""
    global _C_PARSER
    if _C_PARSER is None:
        try:
            import tree_sitter_c
            from tree_sitter import Language, Parser

            _C_PARSER = Parser(Language(tree_sitter_c.language()))
        except Exception:  # noqa: BLE001 - dependência opcional ausente ou incompatível
            _C_PARSER = False
    return _C_PARSER or None


def _declarator_name(node: Any) -> str | None:
    """Nome do identificador mais interno de um declarator (função, ponteiro, array, init)."""
    cur = node
    for _ in range(12):
        if cur is None:
            return None
        if cur.type in ("identifier", "field_identifier", "type_identifier"):
            return cur.text.decode("utf-8", "replace")
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in cur.named_children if c.type.endswith("declarator") or c.type == "identifier"), None)
        cur = nxt
    return None


def _c_symbol(node: Any) -> tuple[str, str] | None:
    """(tipo do trecho, nome) de um nó de nível superior, ou None se não for uma definição relevante."""
    t = node.type
    if t == "function_definition":
        name = _declarator_name(node.child_by_field_name("declarator"))
        return ("c_function", name) if name else None
    if t in ("struct_specifier", "enum_specifier", "union_specifier"):
        name = node.child_by_field_name("name")
        return (f"c_{t.split('_')[0]}", name.text.decode()) if name else None
    if t == "type_definition":
        name = _declarator_name(node.child_by_field_name("declarator"))
        return ("c_typedef", name) if name else None
    if t == "declaration":
        name = _declarator_name(node.child_by_field_name("declarator"))
        body = node.text.count(b"\n")
        if name and body >= 2:  # tabelas/structs estáticas com inicializador
            return ("c_data", name)
        typ = node.child_by_field_name("type")
        if typ is not None and typ.type in ("struct_specifier", "enum_specifier", "union_specifier") and body >= 2:
            tname = typ.child_by_field_name("name")
            return (f"c_{typ.type.split('_')[0]}", tname.text.decode() if tname else (name or "anon"))
        return None
    if t == "preproc_function_def":
        # macros de uma ou duas linhas ficam nos trechos de intervalo (título com os #defines)
        name = node.child_by_field_name("name")
        multiline = node.end_point[0] - node.start_point[0] >= 3
        return ("c_macro", name.text.decode()) if name and multiline else None
    return None


MIN_TAIL_LINES = MAX_CHUNK_LINES // 4  # sobra menor que isso fica na parte anterior


def split_long(rel: str, title: str, ctype: str, lines: list[str], start: int, end: int, chunk: Callable) -> list[Any]:
    """Um trecho [start, end] (1-based, inclusivo), dividido em partes se passar de MAX_CHUNK_LINES.

    Uma sobra curta (fim da função) fica junto da parte anterior: sozinha, ela ganharia do resto no BM25 só por ser
    curta e a busca devolveria 4 linhas de `return 0; }` no lugar da função.
    """
    if end - start + 1 <= MAX_CHUNK_LINES + MIN_TAIL_LINES:
        return [chunk(rel, title, ctype, start, end, "\n".join(lines[start - 1 : end]))]
    bounds: list[list[int]] = []
    for s in range(start, end + 1, MAX_CHUNK_LINES):
        bounds.append([s, min(s + MAX_CHUNK_LINES - 1, end)])
    if len(bounds) > 1 and bounds[-1][1] - bounds[-1][0] + 1 < MIN_TAIL_LINES:
        tail = bounds.pop()
        bounds[-1][1] = tail[1]
    return [
        chunk(rel, f"{title} (part {n})", ctype, s, e, "\n".join(lines[s - 1 : e]))
        for n, (s, e) in enumerate(bounds, start=1)
    ]


_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)")


def gap_chunks(rel: str, name: str, lines: list[str], covered: list[tuple[int, int]], chunk: Callable, ctype: str) -> list[Any]:
    """Trechos para as linhas fora das definições (includes, #defines soltos, comentários)."""
    spans: list[tuple[int, int]] = []
    pos = 1
    for s, e in sorted(covered):
        if s > pos:
            spans.append((pos, s - 1))
        pos = max(pos, e + 1)
    if pos <= len(lines):
        spans.append((pos, len(lines)))
    out: list[Any] = []
    for s, e in spans:
        for ws in range(s, e + 1, GAP_STEP):
            we = min(ws + GAP_STEP - 1, e)
            body = lines[ws - 1 : we]
            if not any(ln.strip() for ln in body):
                continue
            defines = [m.group(1) for ln in body if (m := _DEFINE_RE.match(ln))]
            label = f"{name} (lines {ws}-{we})"
            if defines:
                label += " defines: " + ", ".join(defines[:6]) + ("…" if len(defines) > 6 else "")
            out.append(chunk(rel, label, ctype, ws, we, "\n".join(body)))
    return out


def extract_c_chunks(file_path: Path, rel_path: str, content: str, chunk: Callable) -> list[Any] | None:
    parser = c_parser()
    if parser is None:
        return None
    lines = content.splitlines()
    if not lines:
        return []
    tree = parser.parse(content.encode("utf-8", "replace"))
    chunks: list[Any] = []
    covered: list[tuple[int, int]] = []
    pending_comment: int | None = None
    for node in tree.root_node.children:
        if node.type == "comment":
            # comentário imediatamente antes de uma definição entra no trecho dela
            pending_comment = node.start_point[0] + 1 if pending_comment is None else pending_comment
            continue
        sym = _c_symbol(node)
        if sym is None:
            pending_comment = None
            continue
        ctype, name = sym
        start = node.start_point[0] + 1
        if pending_comment is not None and start - pending_comment <= 40:
            start = pending_comment
        pending_comment = None
        end = node.end_point[0] + 1
        chunks += split_long(rel_path, name, ctype, lines, start, end, chunk)
        covered.append((start, end))
    chunks += gap_chunks(rel_path, file_path.name, lines, covered, chunk, f"text_{file_path.suffix.lstrip('.')}")
    chunks.sort(key=lambda c: c.start_line)
    return chunks


_DTS_NODE_RE = re.compile(r"^\s*(?:([\w,.-]+)\s*:\s*)?(&[\w,.-]+|/|[\w,.@+-]+)\s*\{")


def _strip_dts_line(line: str) -> str:
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)  # strings
    return re.sub(r"//.*$", "", line)


def extract_dts_chunks(file_path: Path, rel_path: str, content: str, chunk: Callable) -> list[Any]:
    lines = content.splitlines()
    if not lines:
        return []
    depth = 0
    in_comment = False
    stack: list[tuple[int, str, int]] = []  # (linha de início, título, profundidade de abertura)
    chunks: list[Any] = []
    covered: list[tuple[int, int]] = []
    root_open = False
    for i, raw in enumerate(lines, start=1):
        line = _strip_dts_line(raw)
        if in_comment:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_comment = False
        line = re.sub(r"/\*.*?\*/", "", line)
        if "/*" in line:
            line = line.split("/*", 1)[0]
            in_comment = True
        m = _DTS_NODE_RE.match(line)
        if m:
            label, name = m.group(1), m.group(2)
            title = f"{label}: {name}" if label else name
            if depth == 0 and name == "/":
                root_open = True
            elif depth == 0 or (depth == 1 and root_open):
                stack.append((i, title, depth))
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if stack and depth == stack[-1][2]:
                    s, title, _ = stack.pop()
                    chunks += split_long(rel_path, title, "dts_node", lines, s, i, chunk)
                    covered.append((s, i))
                if depth == 0:
                    root_open = False
    chunks += gap_chunks(rel_path, file_path.name, lines, covered, chunk, f"text_{file_path.suffix.lstrip('.')}")
    chunks.sort(key=lambda c: c.start_line)
    return chunks
