"""Log local de eventos do ctx (`.ctx/events.jsonl`): base das métricas e da avaliação de eficiência.

Cada comando do ctx grava uma linha com `ts` (epoch), `cmd`, `latency_ms`, `exit` e campos extras que o próprio
comando informa (ex.: `hit` no recall, `steps`/`steps_saved` na receita, `cached` no upstream). Nada sai da máquina e
nenhum conteúdo de arquivo é gravado: só contadores e nomes de comando. Falha de escrita nunca quebra o comando.

100% biblioteca padrão.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ctx.config import load_config

# Campos extras do comando em execução (preenchidos pelos comandos, consumidos e limpos pelo cli.main)
EXTRA: dict[str, Any] = {}


def events_path(workspace_root: Path) -> Path:
    return workspace_root / ".ctx" / "events.jsonl"


def _rotate(path: Path, max_bytes: int) -> None:
    """Mantém só a metade mais recente quando o arquivo passa do limite."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        path.write_text("\n".join(lines[len(lines) // 2 :]) + "\n", encoding="utf-8")
    except OSError:
        pass


def log_event(workspace_root: Path, event: dict[str, Any]) -> None:
    try:
        cfg = load_config(workspace_root)["metrics"]
        if not cfg["events_log"]:
            return
        path = events_path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path, int(cfg["events_max_kb"]) * 1024)
        record = {"ts": round(time.time(), 3), **event}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - métrica é best-effort
        pass


def read_events(workspace_root: Path, since_ts: float = 0.0) -> list[dict[str, Any]]:
    path = events_path(workspace_root)
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and float(ev.get("ts", 0)) >= since_ts:
                out.append(ev)
    except OSError:
        pass
    return out
