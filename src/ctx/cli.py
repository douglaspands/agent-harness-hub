"""CLI principal do motor ctx.

Entry point registrado em [project.scripts] ctx = "ctx.cli:main".
100% biblioteca padrão (argparse, sys, json, time).
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from ctx import WORKSPACE_ROOT
from ctx.policy import doctor, resolve_binaries, sync_rules
from ctx.rag import get_map, index_workspace, search


def format_table(rows: list[list[str]], headers: list[str]) -> str:
    """Formata tabela simples em texto plano para terminal."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * widths[i] for i in range(len(headers)))
    data_lines = [" | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]

    return f"{header_line}\n{sep_line}\n" + "\n".join(data_lines)


def cmd_index(args: argparse.Namespace) -> int:
    res = index_workspace(force=args.force)
    print(f"✓ Indexação concluída em {res['elapsed_ms']}ms:")
    print(f"  • Arquivos indexados/atualizados: {res['indexed']}")
    print(f"  • Arquivos inalterados (pulados): {res['skipped']}")
    print(f"  • Total de arquivos rastreados: {res['total']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    res = search(query=args.query, top_k=args.top)
    print(f"🔍 Busca por '{res['query']}' ({res['count']} resultados em {res['elapsed_ms']}ms):\n")
    if not res["results"]:
        print("Nenhum resultado encontrado no índice FTS5.")
        return 0

    for i, r in enumerate(res["results"], start=1):
        print(f"[{i}] {r['slice']} ({r['chunk_type']}) - BM25 score: {r['score']}")
        print(f"    Título : {r['title']}")
        print(f"    Trecho : {r['snippet']}\n")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    res = get_map()
    print(f"🗺️  Mapa Estrutural do Repositório ({res['files_count']} arquivos, {res['chunks_count']} blocos em {res['elapsed_ms']}ms):\n")
    for path, chunks in sorted(res["tree"].items()):
        print(f"📁 {path}")
        for c in chunks:
            print(f"   ├─ {c['lines']}: {c['title']} ({c['type']})")
    return 0


def cmd_bins(args: argparse.Namespace) -> int:
    res = resolve_binaries(refresh=args.refresh)
    print(f"⚙️  Cache de Binários e Toolchains ({res['elapsed_ms']}ms):\n")

    rows = []
    for tool, info in sorted(res.get("binaries", {}).items()):
        status = "Disponível" if info.get("available") else "Não encontrado"
        path = info.get("path") or "-"
        rows.append([tool, status, path])

    print(format_table(rows, ["Ferramenta", "Status", "Caminho"]))
    return 0


def cmd_rule(args: argparse.Namespace) -> int:
    if args.action == "sync":
        res = sync_rules()
        print(f"✓ Regras sincronizadas com sucesso em {res['elapsed_ms']}ms:")
        for t in res["synced_targets"]:
            print(f"  • {t}")
        return 0
    else:
        print(f"Ação de regra '{args.action}' ainda não implementada diretamente ou use 'sync'.")
        return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    res = doctor()
    status_label = "✅ SAUDÁVEL" if res["all_passed"] else "⚠️  ATENÇÃO NECESSÁRIA"
    print(f"🩺 Diagnóstico do Ambiente ({status_label} em {res['elapsed_ms']}ms):\n")

    rows = []
    for c in res["checks"]:
        tag = "[OK]" if c["passed"] else "[FALHOU]"
        rows.append([tag, c["name"], c["detail"]])

    print(format_table(rows, ["Status", "Verificação", "Detalhe"]))
    return 0 if res["all_passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx",
        description="Motor local de aceleração e governança para Agent Harnesses (RAG, Bins, Policy).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # index
    p_index = subparsers.add_parser("index", help="Indexa incrementalmente o repositório em SQLite FTS5")
    p_index.add_argument("--force", action="store_true", help="Força reindexação completa")
    p_index.set_defaults(func=cmd_index)

    # search
    p_search = subparsers.add_parser("search", help="Busca semântica/léxica cirúrgica no repositório (< 10ms)")
    p_search.add_argument("query", help="Termo ou expressão para busca")
    p_search.add_argument("--top", type=int, default=5, help="Quantidade de resultados (padrão: 5)")
    p_search.set_defaults(func=cmd_search)

    # map
    p_map = subparsers.add_parser("map", help="Exibe o mapa estrutural hierárquico do repositório")
    p_map.set_defaults(func=cmd_map)

    # bins
    p_bins = subparsers.add_parser("bins", help="Lista e atualiza o cache de binários resolvidos (.ctx/bins.json)")
    p_bins.add_argument("--refresh", action="store_true", help="Força nova sondagem de caminhos")
    p_bins.set_defaults(func=cmd_bins)

    # rule
    p_rule = subparsers.add_parser("rule", help="Gerencia e sincroniza regras de governança e guardrails")
    p_rule.add_argument("action", choices=["sync", "allow", "ask", "protect"], help="Ação a executar")
    p_rule.add_argument("target", nargs="?", default="", help="Comando ou caminho alvo")
    p_rule.set_defaults(func=cmd_rule)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Verifica integridade de configurações, hooks e caches")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
