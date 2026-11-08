"""CLI principal do motor ctx (init, prompt, status, index, search, map, bins, rule, metrics, doctor).

Entry point registrado em [project.scripts] ctx = "ctx.cli:main".
100% biblioteca padrão (argparse, sys, json, time).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from ctx import WORKSPACE_ROOT, __version__
from ctx.installer import init_project
from ctx.metrics import analyze, transcripts_dir_for
from ctx.prompts import DEFAULT_SOURCE, add_prompts, list_prompts, remove_prompts, write_catalog
from ctx.policy import doctor, resolve_binaries, sync_rules
from ctx.rag import DEFAULT_PREVIEW_LINES, get_map, index_workspace, search_many, status


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


def cmd_status(args: argparse.Namespace) -> int:
    res = status()
    print(f"📍 Status One-Shot do Workspace ({res['elapsed_ms']}ms):\n")

    git = res["git"]
    print("Git:")
    if git["available"]:
        for line in git["lines"] or ["(sem alterações)"]:
            print(f"  {line}")
    else:
        print("  (não é um repositório git ou git indisponível)")

    spec = res["openspec"]
    print("\nOpenSpec:")
    if not spec["present"]:
        print("  (sem openspec/changes)")
    else:
        if not spec["active"]:
            print("  Nenhuma change ativa.")
        for ch in spec["active"]:
            print(f"  • {ch['name']}: {ch['done']}/{ch['total']} tasks")
            for t in ch["next"]:
                print(f"      ☐ {t}")
        print(f"  Arquivadas: {spec['archived_count']} (ocultas do mapa; use --include-archive)")

    idx = res["index"]
    print(f"\nÍndice FTS5: {idx['files']} arquivos, {idx['chunks']} blocos ({idx['reindexed']} reindexados agora)")

    bins = res["bins"]
    if bins.get("present"):
        print(f"Cache de binários: {bins['available']}/{bins['total']} disponíveis (gerado em {bins['generated_at']})")
    else:
        print("Cache de binários: ausente — execute `ctx rule sync` ou `ctx bins --refresh`")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    res = search_many(args.queries, top_k=args.top, path_prefix=args.path, lines=args.lines)
    scope = f" em '{args.path}'" if args.path else ""
    print(f"🔍 Busca multi-query{scope} ({len(res['queries'])} queries em {res['elapsed_ms']}ms)")

    for q in res["queries"]:
        print(f"\n═══ '{q['query']}' — {q['count']} resultados ═══")
        if not q["results"]:
            print("Nenhum resultado encontrado no índice FTS5.")
            continue
        for i, r in enumerate(q["results"], start=1):
            print(f"\n[{i}] {r['slice']} — {r['title']} ({r['chunk_type']}, BM25 {r['score']})")
            if r["preview"]:
                print(r["preview"])
            else:
                print(f"    {r['snippet']}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    res = get_map(subpaths=args.subpaths, include_archive=args.include_archive)
    scope = f" [{', '.join(args.subpaths)}]" if args.subpaths else ""
    print(f"🗺️  Mapa Estrutural{scope} ({res['files_count']} arquivos, {res['chunks_count']} blocos em {res['elapsed_ms']}ms):\n")
    for path, chunks in res["tree"].items():
        print(f"📁 {path}")
        for c in chunks:
            print(f"   ├─ {c['lines']}: {c['title']} ({c['type']})")
    if res["hidden_archive_files"]:
        print(f"\n({res['hidden_archive_files']} arquivos de openspec/changes/archive/ ocultos; use --include-archive)")
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

    toolchains = res.get("toolchains", {})
    if toolchains:
        print("\n🧰 Toolchains Sandbox-Safe:")
        for name, tc in toolchains.items():
            runners = [tc.get("canonical_runner", "")]
            runners += [f"{r} ({'ok' if info.get('available') else 'ausente'})" for r, info in tc.get("sandbox_runners", {}).items()]
            print(f"  • {name}: {' | '.join(r for r in runners if r)}")

    recipes = res.get("one_shot_recipes", {})
    if recipes:
        print("\n🎯 Receitas One-Shot:")
        for name, cmd in recipes.items():
            print(f"  • {name}: {cmd}")
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


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target) if args.target else Path.cwd()
    res = init_project(target, runner=args.runner, force=args.force, sync=not args.no_sync)
    print(f"📦 Kit agent-harness-hub v{res['version']} em {res['target']} (runner: {res['runner']})\n")
    print(format_table([[a["status"], a["path"]] for a in res["actions"]], ["Ação", "Arquivo"]))
    if "sync" in res:
        print(f"\n✓ Sincronizado: {', '.join(res['sync']['synced_targets'])}")
        doc = res["doctor"]
        failed = [c for c in doc["checks"] if not c["passed"]]
        print("🩺 Doctor: " + ("✅ SAUDÁVEL" if not failed else "⚠️  " + "; ".join(f"{c['name']}: {c['detail']}" for c in failed)))
    print("\nPróximos passos:")
    for i, step in enumerate(res["next_steps"], start=1):
        print(f"  {i}. {step}")
    return 0 if res.get("doctor", {"all_passed": True})["all_passed"] else 1


def cmd_prompt(args: argparse.Namespace) -> int:
    if args.prompt_cmd == "list":
        prompts = list_prompts(args.source)
        rows = [[p["id"], "sim" if p["covered_by_kit"] else "não", p["summary"]] for p in prompts]
        print(format_table(rows, ["ID", "No kit", "Resumo"]))
        return 0
    if args.prompt_cmd == "catalog":
        path = write_catalog(WORKSPACE_ROOT)
        print(f"✓ Catálogo regenerado: {path}")
        return 0

    if args.prompt_cmd == "add":
        report = add_prompts(args.ids, WORKSPACE_ROOT, source=args.source, files=args.file, force=args.force)
    else:
        report = remove_prompts(args.ids, WORKSPACE_ROOT, files=args.file)
    print(format_table([[r["id"], r["file"], r["status"]] for r in report], ["Prompt", "Arquivo", "Resultado"]))
    return 1 if any(r["status"].startswith("não encontrado") for r in report) else 0


def cmd_metrics(args: argparse.Namespace) -> int:
    tdir = Path(args.transcripts).expanduser() if args.transcripts else transcripts_dir_for(WORKSPACE_ROOT)
    if not tdir.is_dir():
        print(f"Diretório de transcripts não encontrado: {tdir}")
        return 1
    res = analyze(tdir)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    print(f"📊 Ferramentas de arquivo: nativo vs shell ({len(res['sessions'])} sessões em {tdir})\n")
    rows = [
        [s["started"], s["session"][:8], s["native_read"], s["bash_read"], s["native_write"], s["bash_write"], s["coach_denies"]]
        for s in res["sessions"][-args.last :]
    ]
    print(format_table(rows, ["Início", "Sessão", "Read", "Leitura shell", "Edit/Write", "Escrita shell", "Deny coach"]))
    t = res["totals"]

    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v}%"

    print(f"\nTotal: leituras via shell {t['bash_read']} ({pct(t['bash_read_pct'])}), escritas via shell {t['bash_write']} ({pct(t['bash_write_pct'])}), deny coach {t['coach_denies']}")
    print("Metas (guia): leituras via shell < 5%, escritas via shell = 0, deny coach caindo ao longo das sessões.")
    return 0


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
    parser.add_argument("--version", action="version", version=f"ctx (agent-harness-hub) {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # index
    p_index = subparsers.add_parser("index", help="Indexa incrementalmente o repositório em SQLite FTS5")
    p_index.add_argument("--force", action="store_true", help="Força reindexação completa")
    p_index.set_defaults(func=cmd_index)

    # status
    p_status = subparsers.add_parser("status", help="Visão One-Shot: Git + OpenSpec + saúde do índice e do cache")
    p_status.set_defaults(func=cmd_status)

    # search
    p_search = subparsers.add_parser("search", help="Busca multi-query FTS5/BM25 com preview de linhas (< 15ms)")
    p_search.add_argument("queries", nargs="+", help="Uma ou mais queries (cada uma é buscada separadamente)")
    p_search.add_argument("--top", type=int, default=5, help="Resultados por query (padrão: 5)")
    p_search.add_argument("--path", default=None, help="Restringe a busca a um subcaminho (ex.: src)")
    p_search.add_argument(
        "--lines", type=int, default=DEFAULT_PREVIEW_LINES, help=f"Linhas de preview por resultado (padrão: {DEFAULT_PREVIEW_LINES}; 0 = só snippet)"
    )
    p_search.set_defaults(func=cmd_search)

    # map
    p_map = subparsers.add_parser("map", help="Mapa estrutural (multi-alvo, sem openspec/changes/archive/)")
    p_map.add_argument("subpaths", nargs="*", help="Subcaminhos a mapear (ex.: src scripts tests)")
    p_map.add_argument("--include-archive", action="store_true", help="Inclui openspec/changes/archive/")
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

    # init
    p_init = subparsers.add_parser("init", help="Instala/atualiza o kit (guardrail, policy, skills, hooks) no projeto atual")
    p_init.add_argument("--target", default=None, help="Diretório do projeto (padrão: diretório atual)")
    p_init.add_argument("--runner", default=None, help="Como os agentes invocam o ctx (padrão: .venv/bin/ctx se houver pyproject.toml, senão ctx)")
    p_init.add_argument("--force", action="store_true", help="Sobrescreve policy.toml e skills existentes")
    p_init.add_argument("--no-sync", action="store_true", help="Só copia os arquivos, sem gerar hooks/instruções")
    p_init.set_defaults(func=cmd_init)

    # prompt
    p_prompt = subparsers.add_parser("prompt", help="Lista, instala e remove prompts individuais do hub")
    prompt_sub = p_prompt.add_subparsers(dest="prompt_cmd", required=True)
    source_help = f"URL https com tag ou diretório local do hub (padrão: {DEFAULT_SOURCE})"
    pl = prompt_sub.add_parser("list", help="Lista os prompts do catálogo")
    pl.add_argument("--source", default=DEFAULT_SOURCE, help=source_help)
    pa = prompt_sub.add_parser("add", help="Instala prompts em AGENTS.md/CLAUDE.md/GEMINI.md (idempotente)")
    pa.add_argument("ids", nargs="+", help="IDs do catálogo (ex.: graph-engineering/dag-fanout-fanin)")
    pa.add_argument("--source", default=DEFAULT_SOURCE, help=source_help)
    pa.add_argument("--file", action="append", help="Arquivo de instruções alvo (repetível; padrão: os existentes)")
    pa.add_argument("--force", action="store_true", help="Instala mesmo se o kit já cobrir o prompt")
    pr = prompt_sub.add_parser("remove", help="Remove o bloco do prompt dos arquivos de instrução")
    pr.add_argument("ids", nargs="+")
    pr.add_argument("--file", action="append")
    prompt_sub.add_parser("catalog", help="(No hub) Regenera prompts/catalog.json a partir dos frontmatters")
    p_prompt.set_defaults(func=cmd_prompt)

    # metrics
    p_metrics = subparsers.add_parser("metrics", help="Mede leituras/escritas de arquivo via shell vs ferramentas nativas (transcripts do Claude Code)")
    p_metrics.add_argument("--transcripts", default=None, help="Diretório de transcripts (padrão: ~/.claude/projects/<projeto>)")
    p_metrics.add_argument("--last", type=int, default=20, help="Quantas sessões mostrar na tabela (padrão: 20)")
    p_metrics.add_argument("--json", action="store_true", help="Saída em JSON")
    p_metrics.set_defaults(func=cmd_metrics)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Verifica integridade de configurações, hooks e caches")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
