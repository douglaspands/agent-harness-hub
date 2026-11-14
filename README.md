# Agent Harness Hub 🎯

Pacotes e prompts prontos para aplicar em qualquer projeto que use agentes de IA (Claude Code, Codex, Cursor, Gemini CLI, Antigravity e afins).

<!-- CATALOG:START — gerado por `ctx prompt catalog`; não edite à mão -->
## 📚 Catálogo

**1 pacote(s)** e **0 prompt(s) avulso(s)** · fonte: branch `main` · catálogo em JSON: [`prompts/catalog.json`](prompts/catalog.json)

### Pacotes

Instalam tudo de uma vez: guardrail, política, instruções e motor `ctx`.

| Pacote | ID | O que faz | Harnesses | Como instalar |
| :--- | :--- | :--- | :--- | :--- |
| [**Autonomia e Segurança**](pacotes/autonomia-e-seguranca/INSTALL.md) | `autonomia-e-seguranca` | Mais autonomia ao agente sem abrir mão da segurança — guardrail com bloqueio real, menos prompts de permissão, Python sempre do .venv, descoberta One-Shot, ferramentas nativas de arquivo e Container-First. | Claude Code, Codex, Cursor, Gemini CLI, Antigravity | [Instalar ↓](#pacote-autonomia-e-seguranca) |

### Prompts avulsos

Prompts independentes, fora dos pacotes, aplicados um a um. Na pasta do projeto, peça ao seu agente:

```text
Instale o prompt <ID> do agent-harness-hub seguindo
https://raw.githubusercontent.com/douglaspands/agent-harness-hub/main/prompts/INSTALL.md
```

O bloco do prompt é inserido em `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` entre marcadores; reinstalar atualiza só esse bloco.
Com o pacote instalado, também dá para usar `.venv/bin/ctx prompt add <ID>`.

_Nenhum prompt avulso ainda._
<!-- CATALOG:END -->

---

<!-- PACKAGE:START — gerado por `ctx prompt catalog`; não edite à mão -->
<a id="pacote-autonomia-e-seguranca"></a>

## 🛡️ Pacote: Autonomia e Segurança

Pacote completo para dar **mais autonomia ao agente sem abrir mão da segurança**. Instala tudo de uma vez: guardrail que bloqueia de verdade o que é perigoso, regras de Turno 0 para o agente acertar na 1ª tentativa e o motor local `ctx`.

### Instalar (tudo de uma vez)

Na pasta do projeto de destino, peça ao seu agente (Claude Code, Codex, Cursor, Gemini CLI, Antigravity…):

```text
Instale o pacote Autonomia e Segurança do agent-harness-hub seguindo
https://raw.githubusercontent.com/douglaspands/agent-harness-hub/main/pacotes/autonomia-e-seguranca/INSTALL.md
Respeite as regras de confirmação do documento e me pergunte antes de alterar
dependências ou qualquer coisa fora do projeto.
```

Ou pelo terminal:

```bash
uv add --dev "agent-harness-hub @ git+https://github.com/douglaspands/agent-harness-hub@main"
.venv/bin/ctx init
```

Sem `uv`: `python3 -m venv .venv`, depois `.venv/bin/python -m pip install "agent-harness-hub @ git+https://github.com/douglaspands/agent-harness-hub@main"` e `.venv/bin/ctx init`. O Python da máquina serve só para criar o venv.

Depois: ajuste `policy.toml` (harnesses do time e comandos de build/teste), rode `.venv/bin/ctx rule sync` e reinicie o agente. No Codex, confie no hook em `/hooks`.

**Extras opcionais** (sem eles tudo funciona e o `ctx index` avisa uma vez o que ficou desligado): `agent-harness-hub[c]` corta C/H por função/struct (tree-sitter); `agent-harness-hub[semantic]` liga a busca semântica local híbrida com o BM25 (model2vec, ~30 MB, baixado só no `ctx index`); `agent-harness-hub[all]` traz os dois. Ex.: `uv add --dev "agent-harness-hub[all] @ git+https://github.com/douglaspands/agent-harness-hub@main"`.

### O que está incluído

- **Guardrail de pré-execução** com bloqueio real no Claude Code, Codex, Cursor e Gemini CLI (e Antigravity): segredos, comandos destrutivos, encadeamento shell, `sudo`, instalação no host, escrita fora do projeto e arquivos de governança.
- **Menos prompts de permissão**: comandos de baixo risco seguem o modo de permissão do próprio harness (`defer`); só o que é sensível pede confirmação.
- **Container-First e Btrfs No-COW** — Serviços e utilitários em containers efêmeros rootless; nada instalado no host sem confirmação; pergunta antes de usar ~/data (No-COW).
- **Descoberta One-Shot em 1 Turno** — Troca varreduras cegas (ls -R, find, grep -r) por ctx status/map/search com preview de 25 linhas no Turno 1.
- **Ferramentas Nativas de Arquivo** — Faz o agente ler, editar e criar arquivos com as ferramentas do harness em vez de cat/head/sed -i/heredoc/redirecionamento no shell.
- **Toolchains Sandbox-Safe e Coaching One-Shot** — Python sempre do .venv (sem uv, python3 só cria o venv), Node via asdf, zero sondagem de binários e receitas One-Shot.
- **Motor `ctx`**: `status`, `map`, `search` multi-query (nunca trava: tempo máximo de 3 s), `doctor`/`doctor --fix`, memória entre sessões (`recall`, `fact`), fontes `upstream` em cache, `recipe` de várias etapas, `metrics` e `evaluate`.

### Quando usar cada comando do `ctx`

| Comando | Quando usar |
| :--- | :--- |
| `ctx status` / `map` / `search "q1" "q2"` | Turno 1: estado do repositório, estrutura e localização de código em 1 chamada. `search` não escreve no índice, para em 3 s (`--timeout`) e devolve o `rg` equivalente se estourar; `--scope code\|reference\|upstream` filtra a origem. Sem trecho com todos os termos, recua para substring (`lane_mb`) e depois para qualquer termo; `[search].glossary` traduz termos do projeto (PT/EN) |
| `ctx doctor` / `ctx doctor --fix` | Algo lento ou estranho: reprova WAL > 64 MB, banco grande, índice atrasado ou busca de teste > 500 ms; `--fix` faz checkpoint, VACUUM e reindex |
| `ctx recall "<pergunta>"` | Sessão retomada ou contexto compactado, antes de repetir comando ou reabrir arquivo: "último comando de build que deu certo", "URL usada para o patch X". Lê só os transcripts locais |
| `ctx fact add` / `search` / `stale` / `review` | Registrar valores medidos, decisões e armadilhas com fonte e data; `stale` lista fatos cuja fonte mudou; `review` aprova candidatos da captura de fim de sessão (opt-in por `[recall].capture_on_stop`) |
| `ctx upstream get <projeto> <versão> <caminho>` | Precisa de um arquivo de um projeto upstream: baixa uma vez para `.ctx/upstream/`, indexa e devolve o caminho local; a mesma versão nunca é baixada duas vezes |
| `ctx recipe list` / `run <nome> [args]` | Rotina de várias etapas repetida (ex.: `copy-verify <origem> <destino>`): uma chamada; cada etapa passa pelo guardrail e uma etapa negada aborta tudo antes de executar |
| `ctx metrics` | Ver por sessão buscas ctx vs desvios `rg`/`grep`/`sed`, latência, chamadas economizadas por receita e acertos do recall |
| `ctx evaluate` (ou `/ctx-avaliar`, ou peça "avalie a eficiência do ctx") | Decidir se o ctx está compensando: veredito por recurso e por modelo, ganho líquido estimado (já descontado o custo das instruções), bloqueios do guardrail por regra e recomendação; abaixo de 30 sessões o veredito sai como provisório |

**Fonte da verdade: a branch `main`.** Instalar ou reinstalar sempre traz o que está na `main`.

**Atualizar um projeto que já tem uma versão anterior:** `uv lock --upgrade-package agent-harness-hub`, `uv sync` e `.venv/bin/ctx upgrade`. É idempotente e não destrutivo: acrescenta ao `policy.toml` só as seções novas (comentadas), troca apenas o bloco gerenciado das skills (preservando o seu texto), atualiza guardrail, hooks, comandos e instruções, e migra ou recria o índice (cache) sozinho. `ctx upgrade --dry-run` mostra antes o que mudaria.

Detalhes, arquitetura e matriz por harness: [`pacotes/autonomia-e-seguranca/GUIA.md`](pacotes/autonomia-e-seguranca/GUIA.md).
<!-- PACKAGE:END -->

---

## ✍️ Como Contribuir

**Novo prompt avulso**

1. Copie [`templates/prompt.md.example`](templates/prompt.md.example) para `prompts/<categoria>/<nome>.md`. O ID será `<categoria>/<nome>`; uma pasta nova vira uma categoria nova.
2. Preencha o frontmatter:
   - `title`: nome curto exibido no catálogo;
   - `summary`: uma frase sobre o que o prompt muda no agente;
   - `harnesses`: lista de harnesses testados;
   - `covered_by_kit`: `true` só se o prompt fizer parte do pacote Autonomia e Segurança (aí ele aparece na lista do pacote, não entre os avulsos);
   - `requires`: `[kit]` se as instruções dependerem do pacote instalado.
3. Escreva a instrução **dentro do bloco** ```` ```markdown ```` da seção `## Snippet de Prompt / Instrução de Sistema`. Só esse bloco é instalado.
4. Rode `uv run ctx prompt catalog` (atualiza `prompts/catalog.json` e este README).
5. Rode `uv run python -m unittest discover -s tests`. Os testes falham se faltar frontmatter ou snippet, se o catálogo/README estiver desatualizado ou se algum documento citar outra versão.

**Novo pacote**

Crie `pacotes/<id>/INSTALL.md` com frontmatter (`title`, `summary`, `harnesses`) e, se quiser, um `GUIA.md`. Ele entra na tabela de pacotes ao rodar `uv run ctx prompt catalog`.

**Publicar**

A branch `main` é a fonte da verdade: não há tags. Abra um PR; ao fazer merge na `main`, o conteúdo já vale para todas as instalações e links do hub. Para registrar a evolução do motor, suba a versão em `pyproject.toml` e `src/ctx/__init__.py` (aparece em `ctx --version`).

<details>
<summary>📁 Estrutura do repositório</summary>

```text
agent-harness-hub/
├── README.md                          # Este índice (seções geradas por `ctx prompt catalog`)
├── pacotes/
│   └── autonomia-e-seguranca/
│       ├── INSTALL.md                 # Instalação completa do pacote (para agentes de IA)
│       └── GUIA.md                    # Arquitetura, racional e matriz por harness
├── prompts/
│   ├── <categoria>/<nome>.md          # Prompts (frontmatter + snippet)
│   ├── catalog.json                   # Catálogo gerado (pacotes + prompts)
│   └── INSTALL.md                     # Instalação de prompts avulsos (para agentes de IA)
├── llms.txt                           # Índice para LLMs (llmstxt.org)
├── templates/                         # prompt.md.example e policy.toml.example
├── src/ctx/                           # Motor ctx: init, prompt, status, search, map, bins, rule, metrics, doctor
│   └── kit/                           # Arquivos instalados pelo `ctx init`
├── scripts/                           # guardrail.py (hook) e venv-python (Python sempre do .venv)
├── tests/                             # Testes do motor, do pacote e do catálogo
├── policy.toml                        # Política deste repositório
└── AGENTS.md / CLAUDE.md / GEMINI.md, .claude/, .codex/, .cursor/, .gemini/, .agents/   # Gerados para este repositório
```

</details>
