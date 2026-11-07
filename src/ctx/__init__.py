"""Módulo central de aceleração para Agent Harnesses (ctx).

Descoberta dinâmica da raiz do workspace, caminhos de cache e definições de ambiente.
100% biblioteca padrão do Python 3.11+ (zero dependências externas).
"""

from __future__ import annotations

from pathlib import Path


def find_workspace_root(start_path: Path | None = None) -> Path:
    """Descobre a raiz do repositório procurando policy.toml, pyproject.toml ou .git."""
    curr = (start_path or Path(__file__)).resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / "policy.toml").exists() or (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return curr.parent.parent


WORKSPACE_ROOT = find_workspace_root()
CTX_DIR = WORKSPACE_ROOT / ".ctx"
INDEX_DB = CTX_DIR / "index.db"
BINS_JSON = CTX_DIR / "bins.json"
POLICY_PATH = WORKSPACE_ROOT / "policy.toml"
