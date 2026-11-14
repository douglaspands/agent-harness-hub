"""Busca semântica local, híbrida com o BM25 (M-05). Extra opcional: `agent-harness-hub[semantic]` (model2vec).

- Embeddings estáticos (model2vec, padrão `minishlab/potion-base-8M`: 256 dimensões, ~30 MB, só CPU) guardados como
  BLOB na tabela `vectors` do próprio index.db; similaridade de cosseno com numpy, por força bruta (milhares de
  trechos em poucos ms). Não usa sqlite-vec: o Python pode vir sem `enable_load_extension`.
- O modelo é baixado SÓ na indexação, direto para `.ctx/models/<nome>` (`snapshot_download(local_dir=…)`, sem a
  cópia duplicada do cache do Hugging Face). A busca só carrega o que já está no disco e nunca acessa a rede.
- Sem model2vec/numpy instalados, `get_model` devolve None e tudo segue só com o BM25.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "minishlab/potion-base-8M"
TEXT_CHARS = 1500  # início do trecho usado no embedding (título e caminho vêm antes)
MODEL_FILES = ["config.json", "model.safetensors", "tokenizer.json", "modules.json", "*.md"]

_MODELS: dict[str, Any] = {}


def available() -> bool:
    """True se a dependência opcional está instalada (não importa nada pesado)."""
    import importlib.util

    return importlib.util.find_spec("model2vec") is not None


def model_dir(workspace_root: Path, name: str) -> Path:
    return workspace_root / ".ctx" / "models" / re.sub(r"[^\w.-]+", "--", name)


def _download(name: str, target: Path) -> None:
    """Baixa os arquivos do modelo direto para `target` (sem cache do HF), de forma atômica."""
    from huggingface_hub import snapshot_download

    tmp = target.with_name(target.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    # só o que o model2vec carrega (o repositório traz também uma cópia ONNX do mesmo tamanho do modelo)
    snapshot_download(repo_id=name, local_dir=str(tmp), allow_patterns=MODEL_FILES)
    shutil.rmtree(tmp / ".cache", ignore_errors=True)  # metadados do download, não do modelo
    os.replace(tmp, target)


def get_model(workspace_root: Path, name: str = DEFAULT_MODEL, allow_download: bool = False) -> Any:
    """Modelo carregado (cache por processo) ou None (dependência ausente, ou ainda não baixado e sem permissão)."""
    key = f"{workspace_root}:{name}:{allow_download}"
    if key in _MODELS:
        return _MODELS[key]
    model = None
    local = model_dir(workspace_root, name)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    try:
        if not local.is_dir() and allow_download and available():
            _download(name, local)
        if local.is_dir():
            from model2vec import StaticModel

            model = StaticModel.from_pretrained(str(local))
    except Exception:  # noqa: BLE001 - semântica é opcional: qualquer falha volta ao BM25 puro
        model = None
    _MODELS[key] = model
    return model


def chunk_text(title: str, path: str, content: str) -> str:
    return f"{title}\n{path}\n{content[:TEXT_CHARS]}"


def encode(model: Any, texts: list[str]) -> Any:
    """Vetores float32 normalizados (norma 1), um por texto."""
    import numpy as np

    vecs = np.asarray(model.encode(texts), dtype=np.float32)
    if vecs.ndim == 1:
        vecs = vecs.reshape(1, -1)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def to_blob(vec: Any) -> bytes:
    return vec.astype("float32").tobytes()


def top_rowids(con: Any, model: Any, query: str, limit: int) -> list[tuple[int, float]]:
    """(rowid do trecho, similaridade de cosseno) dos `limit` trechos mais próximos da consulta."""
    import numpy as np

    rows = con.execute("SELECT id, vec FROM vectors").fetchall()
    if not rows or limit <= 0:
        return []
    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    q = encode(model, [query])[0]
    if q.shape[0] != mat.shape[1]:
        return []  # vetores de outro modelo: `ctx index` refaz
    sims = mat @ q
    k = min(limit, len(rows))
    best = np.argpartition(-sims, k - 1)[:k]
    best = best[np.argsort(-sims[best])]
    return [(int(ids[i]), float(sims[i])) for i in best]
