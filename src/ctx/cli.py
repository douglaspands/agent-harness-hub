"""CLI principal do motor ctx (init, prompt, status, index, search, map, bins, rule, metrics, doctor).

Entry point registrado em [project.scripts] ctx = "ctx.cli:main".
100% biblioteca padrão (argparse, sys, json, time).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from ctx import WORKSPACE_ROOT, __version__, events
from ctx.installer import init_project
from ctx.capture import capture_from_hook, capture_session
from ctx.facts import FactError, add_fact, list_candidates, remove_fact, review_candidates, search_facts, stale_facts
from ctx.recall import PARSERS, recall, sync_recall
from ctx.recipes import RecipeError, list_recipes, run_recipe
from ctx.upstream import UpstreamError, list_upstream, upstream_get

# Comandos que não entram em .ctx/events.jsonl (relatórios sobre o próprio log e instalação)
UNLOGGED_COMMANDS = {"metrics", "evaluate", "init", "upgrade", "prompt", "rule"}
from ctx.evaluate import evaluate, parse_window
from ctx.metrics import analyze, transcripts_dir_for
from ctx.prompts import DEFAULT_SOURCE, add_prompts, list_packages, list_prompts, remove_prompts, write_catalog
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


def humanize_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}min"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def cmd_index(args: argparse.Namespace) -> int:
    res = index_workspace(force=args.force)
    print(f"✓ Indexação concluída em {res['elapsed_ms']}ms:")
    print(f"  • Arquivos indexados/atualizados: {res['indexed']}")
    print(f"  • Arquivos inalterados (pulados): {res['skipped']}")
    print(f"  • Total de arquivos rastreados: {res['total']}")
    print(f"  • WAL após consolidar: {res['wal_bytes']} bytes")
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
    print(f"\nÍndice FTS5: {idx['files']} arquivos, {idx['chunks']} blocos" + (" (reindexado agora)" if idx["reindexed"] else ""))
    size = f"{idx['db_bytes'] / 1024:.0f} KB (+ WAL {idx['wal_bytes'] / 1024:.0f} KB)"
    age = "?" if idx["age_s"] is None else humanize_age(idx["age_s"])
    kinds = ", ".join(f"{k}: {n}" for k, n in sorted(idx["by_kind"].items()))
    print(f"  tamanho: {size} | idade: {age} | por tipo: {kinds or '-'}")
    print(f"  escopo: {idx['scope']}")
    for w in idx["warnings"]:
        print(f"  ⚠ {w}")

    bins = res["bins"]
    if bins.get("present"):
        print(f"Cache de binários: {bins['available']}/{bins['total']} disponíveis (gerado em {bins['generated_at']})")
    else:
        print("Cache de binários: ausente — execute `ctx rule sync` ou `ctx bins --refresh`")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    try:
        res = search_many(args.queries, top_k=args.top, path_prefix=args.path, lines=args.lines, timeout=args.timeout, scope=args.scope)
    except ValueError as e:
        print(f"Erro: {e}")
        return 2
    scope = f" em '{args.path}'" if args.path else ""
    print(f"🔍 Busca multi-query{scope} ({len(res['queries'])} queries em {res['elapsed_ms']}ms)")
    for w in res["warnings"]:
        print(f"⚠ {w}")

    for q in res["queries"]:
        print(f"\n═══ '{q['query']}' — {q['count']} resultados ═══")
        if q["timed_out"]:
            print(f"⏱ Tempo esgotado. Equivalente com rg: {q['rg']}")
        if not q["results"]:
            if not q["timed_out"]:
                print("Nenhum resultado encontrado no índice FTS5.")
            continue
        for i, r in enumerate(q["results"], start=1):
            tag = "" if r["kind"] == "code" else f" [{r['kind']}]"
            print(f"\n[{i}] {r['slice']}{tag} — {r['title']} ({r['chunk_type']}, BM25 {r['score']})")
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
    dry_run = getattr(args, "dry_run", False)
    res = init_project(target, runner=args.runner, force=args.force, sync=not args.no_sync, dry_run=dry_run)
    verb = "Prévia da atualização" if dry_run else "Pacote Autonomia e Segurança"
    print(f"📦 {verb} (agent-harness-hub v{res['version']}) em {res['target']} (runner: {res['runner']})\n")
    print(format_table([[a["status"], a["path"]] for a in res["actions"]], ["Ação", "Arquivo"]))
    if dry_run and res["policy"]["diff"]:
        print("\nSeções que seriam acrescentadas ao policy.toml:\n" + res["policy"]["diff"])
    for w in res.get("index_warnings", []):
        print(f"⚠ {w}")
    if "sync" in res:
        print(f"\n✓ Sincronizado: {', '.join(res['sync']['synced_targets'])}")
        doc = res["doctor"]
        failed = [c for c in doc["checks"] if not c["passed"]]
        print("🩺 Doctor: " + ("✅ SAUDÁVEL" if not failed else "⚠️  " + "; ".join(f"{c['name']}: {c['detail']}" for c in failed)))
    print("\nPróximos passos:")
    for i, step in enumerate(res["next_steps"], start=1):
        print(f"  {i}. {step}")
    return 0 if res.get("doctor", {"all_passed": True})["all_passed"] else 1


def print_hit(i: int, h: dict[str, Any]) -> None:
    mark = {True: "✓", False: "✗", None: "·"}[h["ok"]]
    label = f"{h['tool']}" if h["kind"] == "call" else h["kind"]
    print(f"\n[{i}] {h['when']} {mark} {label} (sessão {h['session'][:8]}, {h['src'].rsplit('/', 1)[-1]})")
    if h["command"]:
        print(f"    $ {h['command'][:300]}")
    if h["text"]:
        print(f"    {h['text'][:300]}")
    if h["result"] and h["kind"] == "call":
        print("    → " + h["result"][:300].replace("\n", "\n      "))
    for u in h["urls"]:
        print(f"    🔗 {u}")


def cmd_recall(args: argparse.Namespace) -> int:
    words = args.question
    if words[:1] == ["capture"]:
        res = capture_session(WORKSPACE_ROOT, Path(args.file).expanduser() if args.file else None, force=not args.from_hook) if not args.from_hook else capture_from_hook(WORKSPACE_ROOT, sys.stdin.read())
        if not args.from_hook:
            print(f"🧠 Captura: {res['status']} — {res.get('added', 0)} candidato(s) novo(s). Revise com `ctx fact review`.")
        return 0
    if words[:1] == ["index"]:
        res = sync_recall(WORKSPACE_ROOT, rebuild=args.rebuild)
        print(f"🧠 Recall: {res['added']} entradas novas em {res['scanned']}/{res['files']} transcripts ({res['elapsed_ms']}ms)")
        for h in res["unsupported_harnesses"]:
            print(f"  ⚠ harness '{h}' ainda sem parser de transcript (suportados: {', '.join(sorted(PARSERS))})")
        return 0
    question = " ".join(words).strip()
    if not question:
        print("Uso: ctx recall \"<pergunta>\" (ex.: \"último comando de build que deu certo\", \"URL usada para o patch X\")")
        return 2
    try:
        res = recall(WORKSPACE_ROOT, question, top=args.top, only_ok=True if args.only_ok else None, tool=args.tool,
                     kind=args.kind, since=args.since, order=args.order, sync=not args.no_sync, raw=args.raw)
    except ValueError as e:
        print(f"Erro: {e}")
        return 2
    events.EXTRA.update({"hit": bool(res["hits"]), "hits": len(res["hits"])})
    flags = [f for f, on in (("só sucesso", res["intent"]["only_ok"]), ("só erros", res["intent"]["only_err"]), ("mais recentes", res["intent"]["recent"]), ("URLs", res["intent"]["urls"])) if on]
    print(f"🧠 Recall '{question}' — {len(res['hits'])} resultado(s) em {res['elapsed_ms']}ms" + (f" [{', '.join(flags)}]" if flags else ""))
    sync = res["sync"]
    if sync and sync["partial"]:
        print("⚠ indexação dos transcripts parcial (limite de tempo); repita para continuar")
    if sync and sync["files"] == 0:
        print("⚠ nenhum transcript encontrado em [recall].sources (padrão: ~/.claude/projects/<projeto>/*.jsonl)")
    for i, h in enumerate(res["hits"], start=1):
        print_hit(i, h)
    if not res["hits"]:
        print("Nada encontrado. Tente termos mais curtos ou --raw.")
    return 0


def cmd_fact(args: argparse.Namespace) -> int:
    try:
        if args.fact_cmd == "add":
            res = add_fact(WORKSPACE_ROOT, args.text, args.source, args.tags)
            print(f"{'= já existia' if res['duplicate'] else '✓ registrado'}: fato #{res['id']} (fonte {res['source_kind']}: {res['source']})")
            return 0
        if args.fact_cmd == "search":
            facts = search_facts(WORKSPACE_ROOT, " ".join(args.query), tag=args.tag, top=args.top)
            events.EXTRA.update({"hit": bool(facts), "hits": len(facts)})
            print(f"📚 {len(facts)} fato(s)")
            for f in facts:
                warn = {"stale": "  ⚠ FONTE MUDOU", "missing": "  ⚠ FONTE REMOVIDA"}.get(f["status"], "")
                print(f"\n#{f['id']} [{f['when']}] {' '.join('#' + t for t in f['tags'])}{warn}\n   {f['text']}\n   fonte: {f['source']}")
            return 0
        if args.fact_cmd == "stale":
            res = stale_facts(WORKSPACE_ROOT)
            print(f"⚠ {len(res['stale'])} fato(s) com fonte alterada/removida ({len(res['ok'])} ok, {len(res['unverifiable'])} de URL não verificáveis)")
            for f in res["stale"]:
                print(f"\n#{f['id']} [{f['when']}] {f['status_detail']}\n   {f['text']}\n   fonte: {f['source']}")
            if res["unverifiable"]:
                print("\nFontes web (sem rede; confira por data):")
                for f in res["unverifiable"]:
                    print(f"  #{f['id']} [{f['when']}] {f['source']}")
            return 1 if res["stale"] else 0
        if args.fact_cmd == "rm":
            ok = remove_fact(WORKSPACE_ROOT, args.id)
            print("✓ removido" if ok else "fato não encontrado")
            return 0 if ok else 1
        if args.fact_cmd == "review":
            if args.accept or args.reject:
                for r in review_candidates(WORKSPACE_ROOT, args.accept or [], args.reject or []):
                    print(f"  #{r['id']}: {r['result']}")
                return 0
            cands = list_candidates(WORKSPACE_ROOT)
            print(f"📝 {len(cands)} candidato(s) pendente(s) (aceite com `ctx fact review --accept ID...`, descarte com `--reject ID...`)")
            for c in cands:
                print(f"\n#{c['id']} [{c['when']}] {' '.join('#' + t for t in c['tags'])}\n   {c['text']}\n   fonte: {c['source']}")
            return 0
    except FactError as e:
        print(f"Erro: {e}")
        return 1
    return 2


def cmd_recipe(args: argparse.Namespace) -> int:
    if args.recipe_cmd == "list":
        rows = [[r["name"], " ".join(f"<{p}>" for p in r["params"]) or "-", str(r["steps"]), r["description"]] for r in list_recipes(WORKSPACE_ROOT)]
        print("🍳 Receitas (`ctx recipe run <nome> [args]`):\n")
        print(format_table(rows, ["Nome", "Argumentos", "Etapas", "Descrição"]))
        return 0
    try:
        res = run_recipe(WORKSPACE_ROOT, args.name, args.args, yes=args.yes, step_timeout=args.timeout)
    except RecipeError as e:
        print(f"Erro: {e}")
        return 2
    events.EXTRA.update(
        {"recipe": args.name, "steps": res["total_steps"], "executed": res["executed"], "steps_saved": res["steps_saved"], "status": res["status"]}
    )
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif res["status"] == "denied":
        print(f"⛔ Receita '{args.name}': a etapa {res['failed_step']} (`{res['command']}`) foi negada pelo guardrail — nada foi executado.")
        print(f"   Motivo: {res['reason']}")
    elif res["status"] == "needs_confirmation":
        print(f"✋ Receita '{args.name}': {len(res['pending'])} etapa(s) não liberadas pelo guardrail — nada foi executado:")
        for p in res["pending"]:
            print(f"   etapa {p['step']} [{p['decision']}] `{p['command']}` — {p['reason']}")
        print("   Se o usuário aprovar, reexecute com --yes (o guardrail pedirá a confirmação dele) ou adicione os comandos a commands.allow no policy.toml.")
    else:
        icon = "✅" if res["status"] == "ok" else "❌"
        print(f"{icon} Receita '{args.name}': {res['status'].upper()} — {res['executed']}/{res['total_steps']} etapas em 1 chamada ({res['steps_saved']} chamadas economizadas)")
        for s in res["steps"]:
            print(f"  {s['n']}. `{s['command']}` → {s['exit']} ({s['ms']}ms)")
            if s["output"] and (s["exit"] != 0 or s is res["steps"][-1]):
                print("     " + s["output"].replace("\n", "\n     "))
    return {"ok": 0, "failed": 1, "denied": 3, "needs_confirmation": 4}[res["status"]]


def cmd_upstream(args: argparse.Namespace) -> int:
    if args.upstream_cmd == "list":
        res = list_upstream(WORKSPACE_ROOT)
        print("🌐 Projetos upstream declarados no policy.toml:")
        for name, p in res["projects"].items():
            print(f"  • {name}: {p['base_url'] or '(por versão)'}  versões: {', '.join(p['versions']) or '-'}")
        if not res["projects"]:
            print("  (nenhum — veja a seção [upstream.*] do policy.toml)")
        print("\n📦 Em cache (.ctx/upstream):")
        for c in res["cached"]:
            print(f"  • {c['key']}  ({c['size']} bytes, {c['fetched_at']})")
        return 0
    try:
        res = upstream_get(WORKSPACE_ROOT, args.project, args.version, args.path, refresh=args.refresh)
    except UpstreamError as e:
        print(f"Erro: {e}")
        return 1
    events.EXTRA.update({"cached": res["cached"], "project": args.project})
    print(res["local_path"])
    print(f"({'cache local, sem rede' if res['cached'] else 'baixado agora'}; {res['size']} bytes; sha256 {res['sha256'][:12]}…; indexado — busque com `ctx search --scope upstream`)", file=sys.stderr)
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    if args.prompt_cmd == "list":
        packages = list_packages(args.source)
        if packages:
            print("📦 Pacotes (instalam tudo de uma vez: siga o INSTALL.md do pacote ou rode `ctx init`)\n")
            print(format_table([[p["id"], p["summary"]] for p in packages], ["ID", "Resumo"]))
            print()
        prompts = [p for p in list_prompts(args.source) if not p["covered_by_kit"]]
        print("🧩 Prompts avulsos (`ctx prompt add <ID>`)\n")
        print(format_table([[p["id"], p["summary"]] for p in prompts], ["ID", "Resumo"]))
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


VERDICT_ICON = {"compensa": "✅", "marginal": "🟡", "não compensa": "❌", "sem dados": "❔"}


def cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        res = evaluate(
            WORKSPACE_ROOT,
            transcripts_dir=Path(args.transcripts).expanduser() if args.transcripts else None,
            since=args.since,
            model=args.model,
        )
    except ValueError as e:
        print(f"Erro: {e}")
        return 2
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    o = res["overall"]
    print(f"📈 Avaliação de eficiência do ctx — janela: {res['window']}" + (f", modelo: {res['model_filter']}" if res["model_filter"] else ""))
    print(f"\n{VERDICT_ICON[o['verdict']]} VEREDITO GERAL: {o['verdict'].upper()} ({o['sessions']} sessões)")
    print(
        f"   ganho estimado {o['saved_calls']} chamadas ≈ {o['saved_tokens']} tokens  −  custo ≈ {o['cost_tokens']} tokens "
        f"(instruções {o['overhead_tokens_per_session']}/sessão + bloqueios {o['denial_waste_tokens']})"
    )
    print(f"   saldo líquido estimado: {o['net_tokens']} tokens ({o['net_tokens_per_session']}/sessão)  [tokens = estimativa chars/4]")
    if o.get("confidence") == "provisório" and o["verdict"] != "sem dados":
        print("   ⚠️  veredito PROVISÓRIO: poucas sessões para decidir (ver recomendação)")
    if o.get("denial_rules"):
        print(f"\nBloqueios por regra ({o['denial_repeats']} repetidos na mesma sessão = coaching que não ensinou):")
        print(format_table([[r["count"], f"~{r['tokens']}", r["rule"]] for r in o["denial_rules"][:8]], ["Vezes", "Tokens", "Regra"]))
    print("\nPor recurso:")
    print(format_table([[f"{VERDICT_ICON[f['verdict']]} {f['verdict']}", f["feature"], f["evidence"]] for f in res["features"]], ["Veredito", "Recurso", "Evidência"]))
    if res["models"]:
        print("\nPor modelo (com ctx vs sem ctx; indício, não prova):")
        rows = []
        for m in res["models"]:
            delta = f"{m['calls_delta_pct']:+.1f}% chamadas/sessão" if m["comparison"] == "ok" else m["comparison"]
            rows.append([m["model"], m["sessions"], f"{m['adoption']:.0%}", delta])
        print(format_table(rows, ["Modelo", "Sessões", "Adoção do ctx", "Comparação"]))
    print("\nRecomendação:")
    for r in res["recommendations"]:
        print(f"  • {r}")
    a = res["assumptions"]
    print(f"\nSuposições: {a['tokens_per_call']} tokens por chamada evitada, {a['recall_hit_calls']} chamadas por acerto de recall/fato (ajustáveis em [metrics.evaluate]).")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    tdir = Path(args.transcripts).expanduser() if args.transcripts else transcripts_dir_for(WORKSPACE_ROOT)
    if not tdir.is_dir():
        print(f"Diretório de transcripts não encontrado: {tdir}")
        return 1
    try:
        since_ts = parse_window(args.since)
    except ValueError as e:
        print(f"Erro: {e}")
        return 2
    res = analyze(tdir, WORKSPACE_ROOT, since_ts)
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

    print("\n🧭 Uso do ctx por sessão (buscas ctx vs desvios rg/grep/sed, memória, receitas e desperdício)\n")
    ctx_rows = []
    for s in res["sessions"][-args.last :]:
        ev = s.get("ctx_events", {})
        ctx_rows.append(
            [
                s["session"][:8], (s["model"] or "-").replace("claude-", "")[:14],
                f"{s['ctx_search']} ({s['ctx_queries']}q)", s["rg_detours"],
                f"{ev.get('recall_hits', 0)}/{ev.get('recall_queries', 0)}", s["ctx_fact"],
                f"{ev.get('upstream_cached', 0)}/{ev.get('upstream_gets', 0)}",
                f"{s['ctx_recipe']} (-{ev.get('recipe_steps_saved', 0)})",
                s["repeated_cmds"], s["redownloads"], s["denials"],
            ]
        )
    print(format_table(ctx_rows, ["Sessão", "Modelo", "ctx search", "rg/grep/sed", "Recall acertos", "Fatos", "Upstream cache", "Receitas (-chamadas)", "Repetidos", "Re-downloads", "Bloqueios"]))
    print(f"\nTotal: ctx search {t['ctx_search']} vs desvios {t['rg_detours']} ({pct(t['ctx_search_pct'])} via ctx)")
    ev = res.get("events", {})
    if ev:
        print(
            f"Recall: {ev['recall_hits']}/{ev['recall_queries']} consultas com resultado | Fatos: {ev['fact_hits']}/{ev['fact_searches']} | "
            f"Upstream: {ev['upstream_cached']}/{ev['upstream_gets']} do cache | Receitas: {ev['recipe_runs']} execuções, {ev['recipe_steps_saved']} chamadas economizadas | Dicas de receita: {ev['hints']}"
        )
        if ev["latency"]:
            print("\n⏱ Latência por comando do ctx:")
            print(format_table([[c, v["n"], v["p50_ms"], v["p95_ms"], v["max_ms"]] for c, v in ev["latency"].items()], ["Comando", "n", "p50 ms", "p95 ms", "máx ms"]))
    print("\nPara saber se tudo isso compensa: `ctx evaluate` (ou peça ao agente: \"avalie a eficiência do ctx\").")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    res = doctor(fix=args.fix)
    status_label = "✅ SAUDÁVEL" if res["all_passed"] else "⚠️  ATENÇÃO NECESSÁRIA"
    if "fix" in res:
        f = res["fix"]
        print("🔧 Correção do índice:")
        for step in f["steps"]:
            print(f"  • {step}")
        print(
            f"  banco {f['before']['db'] / 1024:.0f} KB → {f['after']['db'] / 1024:.0f} KB | "
            f"WAL {f['before']['wal'] / 1024:.0f} KB → {f['after']['wal'] / 1024:.0f} KB\n"
        )
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
    p_search.add_argument("--timeout", type=float, default=None, help="Tempo máximo em segundos (padrão: [search].timeout_s = 3); ao estourar, devolve o rg equivalente")
    p_search.add_argument("--scope", default="all", help="all (padrão), code, reference ou upstream")
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
    p_init = subparsers.add_parser("init", help="Instala/atualiza o pacote Autonomia e Segurança (guardrail, policy, skills, hooks) no projeto atual")
    p_init.add_argument("--target", default=None, help="Diretório do projeto (padrão: diretório atual)")
    p_init.add_argument("--runner", default=None, help="Como os agentes invocam o ctx (padrão: .venv/bin/ctx se houver pyproject.toml, senão ctx)")
    p_init.add_argument("--force", action="store_true", help="Sobrescreve policy.toml e skills existentes")
    p_init.add_argument("--no-sync", action="store_true", help="Só copia os arquivos, sem gerar hooks/instruções")
    p_init.set_defaults(func=cmd_init)

    # upgrade: mesmo caminho do init, voltado a projetos que já têm uma versão anterior
    p_up = subparsers.add_parser(
        "upgrade",
        help="Atualiza um projeto com versão anterior do kit (idempotente, não destrutivo; migra policy, skills, hooks e índice)",
    )
    p_up.add_argument("--target", default=None, help="Diretório do projeto (padrão: diretório atual)")
    p_up.add_argument("--runner", default=None, help="Runner do ctx (padrão: o de [ctx].runner do policy.toml)")
    p_up.add_argument("--dry-run", action="store_true", help="Só mostra o que mudaria, sem escrever nada")
    p_up.add_argument("--force", action="store_true", help="Sobrescreve policy.toml e skills (perde customizações)")
    p_up.add_argument("--no-sync", action="store_true", help="Só copia os arquivos, sem gerar hooks/instruções/índice")
    p_up.set_defaults(func=cmd_init)

    # recall
    p_recall = subparsers.add_parser(
        "recall",
        help="Memória entre sessões: busca nos transcripts locais (ex.: ctx recall \"último comando de build que deu certo\")",
    )
    p_recall.add_argument("question", nargs="*", help="Pergunta; ou `capture` (candidatos a fato) / `index` (atualiza o índice de transcripts)")
    p_recall.add_argument("--top", type=int, default=5)
    p_recall.add_argument("--only-ok", action="store_true", help="Só chamadas que deram certo")
    p_recall.add_argument("--tool", default=None, help="Filtra por ferramenta (ex.: Bash)")
    p_recall.add_argument("--kind", choices=["call", "user", "assistant"], default=None)
    p_recall.add_argument("--since", default=None, help="Janela (ex.: 12h, 7d, 2w)")
    p_recall.add_argument("--order", choices=["rank", "recent"], default=None)
    p_recall.add_argument("--raw", action="store_true", help="Não interpreta a pergunta (usa todas as palavras)")
    p_recall.add_argument("--no-sync", action="store_true", help="Não atualiza o índice de transcripts antes de buscar")
    p_recall.add_argument("--rebuild", action="store_true", help="(index) Recria o índice de transcripts do zero")
    p_recall.add_argument("--file", default=None, help="(capture) Transcript específico (padrão: o mais recente)")
    p_recall.add_argument("--from-hook", action="store_true", help="(capture) Lê o evento do hook de parada no stdin; silencioso")
    p_recall.set_defaults(func=cmd_recall)

    # fact
    p_fact = subparsers.add_parser("fact", help="Fatos verificados com fonte e data (valores medidos, decisões, armadilhas)")
    fact_sub = p_fact.add_subparsers(dest="fact_cmd", required=True)
    fa = fact_sub.add_parser("add", help="Registra um fato com fonte verificável")
    fa.add_argument("text")
    fa.add_argument("--source", required=True, help="arquivo[:linha] existente ou URL https")
    fa.add_argument("--tags", default="", help="Tags separadas por vírgula")
    fs = fact_sub.add_parser("search", help="Busca fatos (aponta os que têm a fonte alterada)")
    fs.add_argument("query", nargs="*")
    fs.add_argument("--tag", default=None)
    fs.add_argument("--top", type=int, default=10)
    fact_sub.add_parser("stale", help="Lista fatos cuja fonte mudou (ou sumiu) depois do registro")
    fr = fact_sub.add_parser("rm", help="Remove um fato")
    fr.add_argument("id", type=int)
    fv = fact_sub.add_parser("review", help="Revisa candidatos gerados pela captura de fim de sessão")
    fv.add_argument("--accept", type=int, nargs="+", metavar="ID")
    fv.add_argument("--reject", type=int, nargs="+", metavar="ID")
    p_fact.set_defaults(func=cmd_fact)

    # recipe
    p_rec =subparsers.add_parser("recipe", help="Receitas de várias etapas: uma chamada em vez de uma por etapa (cada etapa passa pelo guardrail)")
    rec_sub = p_rec.add_subparsers(dest="recipe_cmd", required=True)
    rec_sub.add_parser("list", help="Lista as receitas disponíveis com descrição")
    rr = rec_sub.add_parser("run", help="Executa uma receita (ex.: copy-verify <origem> <destino>)")
    rr.add_argument("name")
    rr.add_argument("args", nargs="*", help="Argumentos posicionais da receita")
    rr.add_argument("--yes", action="store_true", help="Executa também etapas ask/defer (o guardrail pede confirmação do usuário)")
    rr.add_argument("--timeout", type=float, default=120.0, help="Timeout por etapa em segundos (padrão: 120)")
    rr.add_argument("--json", action="store_true", help="Saída em JSON")
    p_rec.set_defaults(func=cmd_recipe)

    # upstream
    p_up_src =subparsers.add_parser("upstream", help="Fontes upstream fixadas por versão: baixa uma vez, indexa e devolve o caminho local")
    up_sub = p_up_src.add_subparsers(dest="upstream_cmd", required=True)
    ug = up_sub.add_parser("get", help="Baixa <caminho> de <projeto>@<versão> para .ctx/upstream (cache; nunca baixa duas vezes)")
    ug.add_argument("project")
    ug.add_argument("version")
    ug.add_argument("path")
    ug.add_argument("--refresh", action="store_true", help="Baixa de novo mesmo que já esteja em cache")
    up_sub.add_parser("list", help="Projetos declarados e arquivos em cache")
    p_up_src.set_defaults(func=cmd_upstream)

    # prompt
    p_prompt = subparsers.add_parser("prompt", help="Lista, instala e remove prompts individuais do hub")
    prompt_sub = p_prompt.add_subparsers(dest="prompt_cmd", required=True)
    source_help = f"URL https do hub ou diretório local (padrão: branch main, {DEFAULT_SOURCE})"
    pl = prompt_sub.add_parser("list", help="Lista os prompts do catálogo")
    pl.add_argument("--source", default=DEFAULT_SOURCE, help=source_help)
    pa = prompt_sub.add_parser("add", help="Instala prompts em AGENTS.md/CLAUDE.md/GEMINI.md (idempotente)")
    pa.add_argument("ids", nargs="+", help="IDs do catálogo (ex.: tooling/ferramentas-nativas-de-arquivo)")
    pa.add_argument("--source", default=DEFAULT_SOURCE, help=source_help)
    pa.add_argument("--file", action="append", help="Arquivo de instruções alvo (repetível; padrão: os existentes)")
    pa.add_argument("--force", action="store_true", help="Instala mesmo se o kit já cobrir o prompt")
    pr = prompt_sub.add_parser("remove", help="Remove o bloco do prompt dos arquivos de instrução")
    pr.add_argument("ids", nargs="+")
    pr.add_argument("--file", action="append")
    prompt_sub.add_parser("catalog", help="(No hub) Regenera prompts/catalog.json a partir dos frontmatters")
    p_prompt.set_defaults(func=cmd_prompt)

    # metrics
    p_eval = subparsers.add_parser(
        "evaluate",
        help="Avalia se o ctx está compensando (chamadas/tokens economizados vs custo), por recurso e por modelo, com recomendação",
    )
    p_eval.add_argument("--since", default=None, help="Janela de análise (ex.: 12h, 30d, 4w; padrão: todas as sessões)")
    p_eval.add_argument("--model", default=None, help="Só sessões cujo modelo contém este texto (ex.: haiku)")
    p_eval.add_argument("--transcripts", default=None, help="Diretório de transcripts (padrão: ~/.claude/projects/<projeto>)")
    p_eval.add_argument("--json", action="store_true", help="Saída em JSON")
    p_eval.set_defaults(func=cmd_evaluate)

    p_metrics = subparsers.add_parser("metrics", help="Mede o uso do ctx por sessão: buscas vs desvios, recall, receitas, latência, ferramentas de arquivo (transcripts do Claude Code)")
    p_metrics.add_argument("--since", default=None, help="Janela de análise (ex.: 12h, 7d, 2w)")
    p_metrics.add_argument("--transcripts", default=None, help="Diretório de transcripts (padrão: ~/.claude/projects/<projeto>)")
    p_metrics.add_argument("--last", type=int, default=20, help="Quantas sessões mostrar na tabela (padrão: 20)")
    p_metrics.add_argument("--json", action="store_true", help="Saída em JSON")
    p_metrics.set_defaults(func=cmd_metrics)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Verifica integridade de configurações, hooks e caches")
    p_doc.add_argument("--fix", action="store_true", help="Conserta o índice antes: checkpoint do WAL, VACUUM e reindex")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    events.EXTRA.clear()
    started = time.perf_counter()
    code = args.func(args)
    if args.command not in UNLOGGED_COMMANDS:
        sub = getattr(args, f"{args.command}_cmd", None)
        events.log_event(
            WORKSPACE_ROOT,
            {
                "cmd": f"{args.command} {sub}" if sub else args.command,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "exit": code,
                **events.EXTRA,
            },
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
