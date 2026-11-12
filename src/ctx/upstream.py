"""Fontes upstream fixadas por versão (`ctx upstream get <projeto> <versão> <caminho>`).

Baixa o arquivo UMA vez para `.ctx/upstream/<projeto>/<versão>/<caminho>`, indexa (kind='upstream') e devolve o
caminho local. A mesma (projeto, versão, caminho) nunca é baixada duas vezes: a segunda chamada é cache hit, sem rede.

Os projetos e URLs vêm do `policy.toml` (`[upstream.<projeto>]`); nada é embutido no código:

    [upstream.meu-projeto]
    base_url = "https://raw.githubusercontent.com/org/repo/{version}/{path}"
    [upstream.meu-projeto.versions]          # opcional: URL própria por versão
    v1 = "https://mirror.exemplo.org/repo/v1/{path}"

Segurança: só https, nomes validados, caminho confinado ao diretório da versão (sem `..`, absolutos ou symlinks
que escapem), tamanho máximo, escrita atômica e User-Agent genérico (nenhum dado do projeto vai na requisição).
A única saída de rede do ctx é este comando, sempre explícito.

100% biblioteca padrão.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ctx.config import load_raw_policy
from ctx.rag import index_extra_file, is_binary_file

MAX_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 30
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

_urlopen = urllib.request.urlopen  # ponto de injeção para testes (nenhum teste usa a rede)


class UpstreamError(Exception):
    """Erro de configuração, validação ou download (mensagem pronta para o usuário)."""


def upstream_root(workspace_root: Path) -> Path:
    return workspace_root / ".ctx" / "upstream"


def _manifest_path(workspace_root: Path) -> Path:
    return upstream_root(workspace_root) / "manifest.json"


def load_manifest(workspace_root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_manifest_path(workspace_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(workspace_root: Path, manifest: dict[str, Any]) -> None:
    path = _manifest_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def clean_relative_path(path: str) -> str:
    """Caminho relativo seguro dentro da árvore do projeto upstream (sem `..`, absolutos, backslash ou NUL)."""
    if not path or "\\" in path or "\0" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise UpstreamError(f"caminho inválido: {path!r} (use um caminho relativo, com '/')")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UpstreamError(f"caminho inválido: {path!r} (sem '..')")
    return "/".join(parts)


def resolve_url(policy: dict[str, Any], project: str, version: str, path: str) -> str:
    projects = policy.get("upstream", {})
    cfg = projects.get(project) if isinstance(projects, dict) else None
    if not isinstance(cfg, dict):
        known = ", ".join(sorted(k for k, v in projects.items() if isinstance(v, dict))) or "nenhum"
        raise UpstreamError(f"projeto upstream '{project}' não declarado em [upstream.*] do policy.toml (declarados: {known})")
    template = (cfg.get("versions") or {}).get(version) or cfg.get("base_url")
    if not isinstance(template, str) or "{path}" not in template:
        raise UpstreamError(f"[upstream.{project}] precisa de base_url (ou versions.{version}) com o placeholder {{path}}")
    url = template.replace("{version}", urllib.parse.quote(version, safe="")).replace(
        "{path}", urllib.parse.quote(path, safe="/")
    )
    if urllib.parse.urlparse(url).scheme != "https":
        raise UpstreamError(f"somente https é permitido (URL de '{project}': {url})")
    return url


def _destination(workspace_root: Path, project: str, version: str, path: str) -> Path:
    base = (upstream_root(workspace_root) / project / version).resolve()
    dest = (base / path).resolve()
    if dest == base or base not in dest.parents:
        raise UpstreamError(f"caminho fora do diretório da versão: {path!r}")
    return dest


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ctx-upstream"})
    try:
        with _urlopen(req, timeout=TIMEOUT_S) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        raise UpstreamError(f"HTTP {code} ao baixar {url}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise UpstreamError(f"falha de rede ao baixar {url}: {e}") from e
    if len(data) > MAX_BYTES:
        raise UpstreamError(f"arquivo acima de {MAX_BYTES // (1024 * 1024)} MB: {url}")
    return data


def upstream_get(
    workspace_root: Path, project: str, version: str, path: str, refresh: bool = False
) -> dict[str, Any]:
    """Garante o arquivo em cache local e indexado. Retorna {local_path, cached, url, sha256, size}."""
    for label, value in (("projeto", project), ("versão", version)):
        if not NAME_RE.match(value) or ".." in value:
            raise UpstreamError(f"{label} inválido: {value!r}")
    rel = clean_relative_path(path)
    dest = _destination(workspace_root, project, version, rel)
    key = f"{project}/{version}/{rel}"
    manifest = load_manifest(workspace_root)
    entry = manifest.get(key)

    if entry and dest.is_file() and not refresh:
        _ensure_indexed(workspace_root, dest)
        return {**entry, "local_path": str(dest), "cached": True}

    url = resolve_url(load_raw_policy(workspace_root), project, version, rel)
    data = _download(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    entry = {
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest[key] = entry
    _save_manifest(workspace_root, manifest)
    _ensure_indexed(workspace_root, dest)
    return {**entry, "local_path": str(dest), "cached": False}


def _ensure_indexed(workspace_root: Path, dest: Path) -> None:
    if not is_binary_file(dest, 8192):
        index_extra_file(workspace_root, dest, "upstream")


def list_upstream(workspace_root: Path) -> dict[str, Any]:
    """Projetos declarados e arquivos já em cache."""
    policy = load_raw_policy(workspace_root)
    projects = {
        name: {"base_url": cfg.get("base_url"), "versions": sorted((cfg.get("versions") or {}).keys())}
        for name, cfg in (policy.get("upstream") or {}).items()
        if isinstance(cfg, dict)
    }
    cached = [
        {"key": key, "url": e.get("url"), "size": e.get("size"), "fetched_at": e.get("fetched_at")}
        for key, e in sorted(load_manifest(workspace_root).items())
    ]
    return {"projects": projects, "cached": cached}
