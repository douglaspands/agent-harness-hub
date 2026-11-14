"""Atualização não destrutiva do `policy.toml` de projetos que já têm uma versão anterior do kit.

`ctx upgrade` acrescenta ao FIM do arquivo apenas as seções novas que ainda não existem, como defaults
COMENTADOS (documentação para o usuário descomentar). Nada do que o usuário escreveu é alterado, reordenado
ou reserializado, e o comportamento não muda: os defaults reais vivem em `ctx.config.DEFAULTS`.

Idempotente: cada bloco leva o marcador `# ctx:section <nome>`; blocos já presentes (ou tabelas já definidas
pelo usuário) nunca são acrescentados de novo.

100% biblioteca padrão.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

SECTION_MARKER = "# ctx:section "
UPGRADE_HEADER = "# --- ctx {version}: seções novas (defaults comentados; descomente para customizar) ---"

# nome -> (tabela de topo que, se já existir no policy.toml, dispensa o bloco, texto do bloco)
SECTIONS: dict[str, tuple[str, str]] = {
    "index": (
        "index",
        """# ctx:section index
# [index]
# Escopo do índice (`ctx index`/`ctx search`). Padrões em código; listas aqui SUBSTITUEM a lista padrão.
# respect_gitignore = true            # usa `git ls-files`/.gitignore
# max_file_kb = 1024                  # arquivos maiores não são indexados
# extra_exclude_dirs = ["generated"]  # somam-se aos diretórios de build/cache/venv já excluídos
# exclude_globs = ["*.min.js"]
# journal_size_limit_mb = 8           # teto do arquivo -wal após cada checkpoint
# wal_autocheckpoint_pages = 200
#
# [index.reference]                   # material fora do git que vale buscar (dumps, documentação extraída)
# globs = ["reference/**/*.txt"]      # relativos à raiz do projeto (ou absolutos / com ~)
# max_file_kb = 4096
""",
    ),
    "search": (
        "search",
        """# ctx:section search
# [search]
# timeout_s = 3.0                     # nenhuma busca passa disso; ao estourar devolve o `rg` equivalente
# refresh = "off"                     # "off": a busca nunca escreve no índice | "incremental": atualiza com orçamento
# refresh_budget_ms = 300
""",
    ),
    "doctor": (
        "doctor",
        """# ctx:section doctor
# [doctor]
# wal_max_mb = 64                     # WAL acima disso reprova o `ctx doctor` (`--fix` consolida)
# db_max_mb = 512
# search_max_ms = 500                 # uma busca de teste mais lenta que isso reprova
# stale_check = true                  # reprova se o índice estiver atrasado em relação aos arquivos/git
""",
    ),
    "upstream": (
        "upstream",
        """# ctx:section upstream
# Fontes upstream fixadas: `ctx upstream get <projeto> <versão> <caminho>` baixa UMA vez
# para .ctx/upstream/<projeto>/<versão>/<caminho> e indexa. Declare os projetos aqui (https apenas):
# [upstream.meu-projeto]
# base_url = "https://raw.githubusercontent.com/org/repo/{version}/{path}"   # {version} e {path} obrigatórios
# [upstream.meu-projeto.versions]     # opcional: URL base própria por versão (mesmos placeholders)
# v1 = "https://mirror.exemplo.org/repo/v1/{path}"
""",
    ),
    "recall": (
        "recall",
        """# ctx:section recall
# Memória local entre sessões (`ctx recall`, `ctx fact`). Nada sai da máquina; `paths.deny` é respeitado.
# [recall]
# sources = [{ harness = "claude", path = "~/.claude/projects/{project}/*.jsonl" }]   # {project} = raiz do projeto normalizada
# capture_on_stop = false             # true: hook de parada gera candidatos a fato para `ctx fact review`
# redact = ["(?i)(api[_-]?key|token|secret|password)\\\\s*[=:]\\\\s*\\\\S+"]
""",
    ),
    "recipes": (
        "recipes",
        """# ctx:section recipes
# Receitas de várias etapas (`ctx recipe run <nome> [args]`). Cada etapa passa pelo guardrail; `deny` aborta.
# `copy-verify <origem> <destino>` já vem embutida (copiar, sincronizar, conferir sha256); redefina-a aqui se precisar.
# [recipes.minha-receita]
# description = "o que a receita faz"
# params = ["alvo"]
# requires = ["git status", "rg"]     # comandos de que a receita precisa (`ctx recipe list` mostra os que faltam no allow)
# steps = ["git status -s -b", "rg -n {alvo} src"]
""",
    ),
    "metrics": (
        "metrics",
        """# ctx:section metrics
# [metrics]
# events_log = true                   # .ctx/events.jsonl: latência por comando, receitas e recall (só local)
# events_max_kb = 1024
#
# [metrics.evaluate]                  # limiares e SUPOSIÇÕES de `ctx evaluate` (tokens são estimativas chars/4)
# min_sessions = 5                    # abaixo disso o veredito é "sem dados"
# min_group_sessions = 3              # mínimo por grupo (com/sem ctx) para comparar por modelo
# min_net_saving_tokens = 2000        # ganho líquido estimado por sessão para "compensa"
# tokens_per_call = 250               # custo médio de uma chamada de ferramenta evitada
# recall_hit_calls = 2                # chamadas que um acerto de recall/fato costuma evitar
# search_adoption_ok = 0.5            # fração de buscas via ctx (vs rg/grep/sed) para "compensa"
""",
    ),
    # ctx 0.5: chaves novas em tabelas que já existem; presença testada pela chave (tabela.chave)
    "search-quality": (
        "search.glossary",
        """# ctx:section search-quality
# Qualidade da busca (ctx 0.5): acrescente estas chaves à tabela [search].
# glossary = [["painel", "panel"], ["luz de fundo", "backlight"]]   # termos equivalentes do projeto (PT/EN, apelidos)
# semantic = true                     # semântica híbrida com o BM25; só com o extra agent-harness-hub[semantic]
# semantic_model = "minishlab/potion-base-8M"   # baixado no `ctx index` para .ctx/models/ (a busca nunca baixa)
# C/H por função/struct: extra agent-harness-hub[c]. DTS/DTSI já são cortados por nó, sem dependência.
""",
    ),
    "paths-readonly": (
        "paths.readonly_commands",
        """# ctx:section paths-readonly
# Leitura fora do workspace (ctx 0.5), na tabela [paths]: readonly_system aceita ~ e /** (ex.: "/run/media/**",
# "~/data/projeto/src") e vale também dentro do ~/data, só para leitura. Comandos só de leitura que, com caminhos
# de readonly_system, não pedem confirmação no ~/data:
# readonly_commands = ["rg", "ls", "fd", "sha256sum", "wc", "stat", "tree", "file", "cmp", "diff"]
""",
    ),
    "metrics-confidence": (
        "metrics.evaluate.confident_sessions",
        """# ctx:section metrics-confidence
# Na tabela [metrics.evaluate] (ctx 0.5):
# confident_sessions = 30             # abaixo disso o veredito do `ctx evaluate` sai como "provisório"
""",
    ),
}


def _has_key(parsed: dict[str, Any], dotted: str) -> bool:
    node: Any = parsed
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _present(text: str, parsed: dict[str, Any], name: str, table: str) -> bool:
    return (SECTION_MARKER + name) in text or _has_key(parsed, table)


def missing_sections(text: str) -> list[str]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        parsed = {}
    return [name for name, (table, _block) in SECTIONS.items() if not _present(text, parsed, name, table)]


def upgraded_text(text: str, version: str) -> tuple[str, list[str]]:
    """Texto final do policy.toml e as seções acrescentadas (lista vazia = nada a fazer)."""
    names = missing_sections(text)
    if not names:
        return text, []
    sep = "" if text.endswith("\n") or not text else "\n"
    body = "\n".join(SECTIONS[n][1].rstrip("\n") + "\n" for n in names)
    return f"{text}{sep}\n{UPGRADE_HEADER.format(version=version)}\n{body}", names


def upgrade_policy(workspace_root: Path, version: str, dry_run: bool = False) -> dict[str, Any]:
    """Acrescenta ao policy.toml as seções ausentes. Retorna {status, added, diff}."""
    path = workspace_root / "policy.toml"
    if not path.exists():
        return {"status": "ausente", "added": [], "diff": ""}
    text = path.read_text(encoding="utf-8")
    new_text, added = upgraded_text(text, version)
    if not added:
        return {"status": "inalterado", "added": [], "diff": ""}
    diff = new_text[len(text) :]
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return {"status": f"atualizado (+{', '.join(added)})" if not dry_run else f"atualizaria (+{', '.join(added)})", "added": added, "diff": diff}
