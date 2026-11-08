"""Módulo central de aceleração para Agent Harnesses (ctx).

Descoberta dinâmica da raiz do workspace, caminhos de cache e definições de ambiente.
100% biblioteca padrão do Python 3.11+ (zero dependências externas).
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.3.0"

# A branch main é a fonte da verdade: instalações e documentos sempre apontam para ela (sem tags).
REPO_URL = "https://github.com/douglaspands/agent-harness-hub"
SOURCE_REF = "main"
RAW_BASE = f"https://raw.githubusercontent.com/douglaspands/agent-harness-hub/{SOURCE_REF}"
GIT_SPEC = f"git+{REPO_URL}@{SOURCE_REF}"


def find_workspace_root(start_path: Path | None = None) -> Path:
    """Descobre a raiz do workspace a partir do diretório atual.

    Ordem: variável CTX_WORKSPACE > 1º ancestral com policy.toml > 1º ancestral com .git > diretório inicial.
    Partir do cwd (e não de __file__) permite rodar o ctx instalado como dependência (.venv/bin/ctx)
    ou como ferramenta (uv tool / uvx) dentro de qualquer projeto.
    """
    env_root = os.environ.get("CTX_WORKSPACE")
    if env_root:
        return Path(env_root).expanduser().resolve()

    curr = (start_path or Path.cwd()).resolve()
    chain = [curr, *curr.parents]
    for marker in ("policy.toml", ".git"):
        for parent in chain:
            if (parent / marker).exists():
                return parent
    return curr


WORKSPACE_ROOT = find_workspace_root()
CTX_DIR = WORKSPACE_ROOT / ".ctx"
INDEX_DB = CTX_DIR / "index.db"
BINS_JSON = CTX_DIR / "bins.json"
POLICY_PATH = WORKSPACE_ROOT / "policy.toml"
