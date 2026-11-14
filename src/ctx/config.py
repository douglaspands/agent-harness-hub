"""Configuração do ctx: defaults em código + sobreposição pelo `policy.toml` do projeto.

Toda seção nova (`[index]`, `[search]`, `[doctor]`, `[recall]`, `[metrics]`, ...) é opcional: um policy.toml
de versão anterior continua funcionando sem edição, porque cada chave ausente cai no default daqui.
Nada neste módulo depende de um projeto específico.

100% biblioteca padrão.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "index": {
        "respect_gitignore": True,
        "max_file_kb": 1024,
        "binary_sniff_bytes": 8192,
        "exclude_dirs": [
            ".git", ".ctx", ".venv", "venv", "env", "__pycache__", "node_modules", ".idea", ".vscode",
            "dist", "build", "target", ".tox", ".nox", ".next", ".gradle", ".cache", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", "site-packages",
        ],
        "exclude_globs": [],
        "exclude_extensions": [
            ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".ico",
            ".svg", ".webp", ".db", ".sqlite", ".sqlite3", ".bin", ".tar", ".gz", ".zip", ".whl",
        ],
        # journal_size_limit / wal_autocheckpoint (páginas de 4 KB) e teto de espera do SQLite
        "journal_size_limit_mb": 8,
        "wal_autocheckpoint_pages": 200,
        "busy_timeout_ms": 2000,
        "reference": {"globs": [], "max_file_kb": 4096},
    },
    "search": {
        "timeout_s": 3.0,
        # "off": busca nunca escreve no índice (padrão) | "incremental": atualiza dentro de um orçamento de tempo
        "refresh": "off",
        "refresh_budget_ms": 300,
    },
    "doctor": {
        "wal_max_mb": 64,
        "db_max_mb": 512,
        "search_max_ms": 500,
        "stale_check": True,
    },
    "recall": {
        # {project} = raiz do projeto normalizada (mesmo nome de pasta que o Claude Code usa). Um item por harness.
        "sources": [{"harness": "claude", "path": "~/.claude/projects/{project}/*.jsonl"}],
        "capture_on_stop": False,
        "result_chars": 600,
        "sync_budget_s": 10.0,
        "max_line_kb": 4096,
        # Padrões simples (sem backtracking pesado) aplicados a tudo que é gravado
        "redact": [
            r"(?i)\b(?:api[_-]?key|token|secret|passw(?:or)?d|authorization)\b\s*[=:]\s*\S+",
            r"\bsk-[A-Za-z0-9_-]{16,}",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        ],
        # Programas cujos comandos bem-sucedidos viram candidatos a fato na captura de fim de sessão
        "notable_programs": [
            "make", "cmake", "cargo", "npm", "pnpm", "yarn", "uv", "pip", "pytest", "go", "docker", "podman",
            "gradle", "mvn", "flash", "scp", "rsync", "ssh", "curl", "wget",
        ],
        "max_candidates": 12,
    },
    # Receitas embutidas (genéricas, sem nada de projeto); `[recipes.<nome>]` do policy.toml as sobrescreve.
    "recipes": {
        "copy-verify": {
            "description": "Copia origem -> destino (arquivo), sincroniza o disco e confere o sha256: tudo numa chamada",
            "params": ["origem", "destino"],
            "steps": [
                "cp {origem} {destino}",
                "sync",
                {"run": "sha256sum {origem}", "capture": "hash_origem"},
                {"run": "sha256sum {destino}", "capture": "hash_destino"},
                {"check": "equal", "a": "hash_origem", "b": "hash_destino", "message": "sha256 diverge entre origem e destino"},
            ],
        }
    },
    "metrics": {
        "events_log": True,
        "events_max_kb": 1024,
        # Limiares e SUPOSIÇÕES do `ctx evaluate` (todas as economias são estimativas; ajuste à sua realidade)
        "evaluate": {
            "min_sessions": 5,            # abaixo disso o veredito é "sem dados"
            "min_group_sessions": 3,      # mínimo por grupo (com/sem ctx) para comparar por modelo
            "min_net_saving_tokens": 2000,  # ganho líquido estimado por sessão para "compensa"
            "tokens_per_call": 250,       # custo médio de uma chamada de ferramenta evitada (pedido + resultado + turno)
            "recall_hit_calls": 2,        # chamadas que um acerto de recall/fato costuma evitar (redescobrir + reexecutar)
            "search_adoption_ok": 0.5,    # fração de buscas via ctx (vs rg/grep/sed) para "compensa"
            "confident_sessions": 30,     # abaixo disso o veredito sai como "provisório"
        },
    },
}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_raw_policy(workspace_root: Path) -> dict[str, Any]:
    path = workspace_root / "policy.toml"
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def is_denied(path: Path, policy: dict[str, Any], workspace_root: Path) -> str | None:
    """Entrada de `paths.deny` que casa com `path` (segredos, dados pessoais), ou None.

    Entradas sem "/" casam pelo nome em qualquer diretório (ex.: `.env`); as demais, por prefixo de diretório.
    Usado pelo índice de código e pelo recall para nunca guardar conteúdo negado pela política.
    """
    for entry in policy.get("paths", {}).get("deny", []):
        if "/" not in entry and not entry.startswith("~"):
            if path.name == entry:
                return entry
            continue
        base = Path(entry).expanduser()
        if not base.is_absolute():
            base = workspace_root / base
        try:
            base = base.resolve()
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return entry
    return None


def load_config(workspace_root: Path) -> dict[str, Any]:
    """Política do projeto sobreposta aos defaults (seções ausentes = default)."""
    return deep_merge(DEFAULTS, load_raw_policy(workspace_root))
