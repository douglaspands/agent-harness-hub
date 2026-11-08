# Agent Harness Hub 🎯

Catálogo de **prompts e diretrizes prontos para aplicar** em qualquer projeto que use agentes de IA (Claude Code, Google Antigravity, Codex, Cursor, Aider e afins), para dar **mais autonomia com segurança**: menos turnos desperdiçados, menos prompts de permissão desnecessários e bloqueio do que é perigoso.

Cada prompt é aplicado por **ID** com uma frase no chat do seu agente. Veja abaixo como aplicar e o catálogo completo.

<!-- APPLY:START — gerado por `ctx prompt catalog`; não edite à mão -->
## 🚀 Como Aplicar um Prompt

1. Escolha o **ID** de um ou mais prompts no [catálogo](#-catálogo-de-prompts).
2. Na pasta do projeto de destino, peça ao seu agente (Claude Code, Antigravity, Codex, Cursor…):

   ```text
   Instale o prompt <ID> do agent-harness-hub seguindo
   https://raw.githubusercontent.com/douglaspands/agent-harness-hub/v0.2.0/prompts/INSTALL.md
   ```

   Para vários prompts, liste os IDs separados por espaço.
3. Reinicie a sessão do agente para ele carregar as instruções novas.

Pelo terminal, em projetos com o kit: `.venv/bin/ctx prompt add <ID> [<ID> ...]` (listar: `ctx prompt list`; remover: `ctx prompt remove <ID>`).

**O que acontece:** só o bloco de snippet do prompt é inserido em `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` (os que existirem; senão é criado o `AGENTS.md`, lido por Codex, Cursor, Copilot e outros), entre marcadores `<!-- agent-harness-hub:prompt <ID> ... -->`. Reinstalar atualiza o bloco; nada fora dele é alterado.

**Por que a URL tem tag (`v0.2.0`) e não `main`:** o conteúdo fica imutável e auditável; um commit futuro na `main` não muda o que o seu agente obedece.

### Kit completo (bloqueio real, não só instruções)

Além dos prompts, o hub tem um kit com guardrail de pré-execução (bloqueio real no Claude Code, Codex, Cursor e Gemini CLI), `policy.toml`, motor de busca local `ctx` e skills. Ele já inclui os prompts marcados com ✅ na coluna **Kit**, e é pré-requisito dos prompts com `kit` na coluna **Requer**:

```text
Leia https://raw.githubusercontent.com/douglaspands/agent-harness-hub/v0.2.0/BOOTSTRAP.md e siga as instruções
para instalar o kit agent-harness-hub neste projeto. Respeite as regras de confirmação do documento e
me pergunte antes de alterar dependências ou qualquer coisa fora do projeto.
```
<!-- APPLY:END -->

<!-- CATALOG:START — gerado por `ctx prompt catalog`; não edite à mão -->
## 📚 Catálogo de Prompts

**5 prompts** · versão `v0.2.0` · catálogo em JSON: [`prompts/catalog.json`](prompts/catalog.json)

### `discovery`

| Prompt | ID para aplicar | O que faz | Harnesses | Requer | Kit |
| :--- | :--- | :--- | :--- | :---: | :---: |
| [**Descoberta One-Shot em 1 Turno**](prompts/discovery/descoberta-em-1-turno.md) | `discovery/descoberta-em-1-turno` | Troca varreduras cegas (ls -R, find, grep -r) por ctx status/map/search com preview de 25 linhas no Turno 1. | Claude Code, Antigravity, Codex, Cursor, Aider | kit | ✅ |

### `governance-and-guardrails`

| Prompt | ID para aplicar | O que faz | Harnesses | Requer | Kit |
| :--- | :--- | :--- | :--- | :---: | :---: |
| [**Container-First e Btrfs No-COW**](prompts/governance-and-guardrails/container-first-e-btrfs.md) | `governance-and-guardrails/container-first-e-btrfs` | Serviços e utilitários em containers efêmeros rootless; nada instalado no host sem confirmação; pergunta antes de usar ~/data (No-COW). | Claude Code, Antigravity, Codex, Cursor | — | ✅ |

### `graph-engineering`

| Prompt | ID para aplicar | O que faz | Harnesses | Requer | Kit |
| :--- | :--- | :--- | :--- | :---: | :---: |
| [**Graph Engineering (DAG Fan-Out + Fan-In)**](prompts/graph-engineering/dag-fanout-fanin.md) | `graph-engineering/dag-fanout-fanin` | Proíbe encadeamento shell (&&, ;, &) e estrutura paralelismo em chamadas atômicas no mesmo turno ou DAG de subagentes. | Claude Code, Antigravity, Swarm, LangGraph, AutoGen | — | ✅ |

### `toolchains-and-executors`

| Prompt | ID para aplicar | O que faz | Harnesses | Requer | Kit |
| :--- | :--- | :--- | :--- | :---: | :---: |
| [**Toolchains Sandbox-Safe e Coaching One-Shot**](prompts/toolchains-and-executors/enforcement-toolchains-canonicos.md) | `toolchains-and-executors/enforcement-toolchains-canonicos` | Python sempre do .venv (sem uv, python3 só cria o venv), Node via asdf, zero sondagem de binários e receitas One-Shot. | Claude Code, Antigravity, Codex, Cursor | kit | ✅ |

### `tooling`

| Prompt | ID para aplicar | O que faz | Harnesses | Requer | Kit |
| :--- | :--- | :--- | :--- | :---: | :---: |
| [**Ferramentas Nativas de Arquivo**](prompts/tooling/ferramentas-nativas-de-arquivo.md) | `tooling/ferramentas-nativas-de-arquivo` | Faz o agente ler, editar e criar arquivos com as ferramentas do harness em vez de cat/head/sed -i/heredoc/redirecionamento no shell. | Claude Code, Antigravity, Codex, Cursor | — | ✅ |
<!-- CATALOG:END -->

---

## 📖 Guias

| Guia | Para quê |
| :--- | :--- |
| [RAG Local One-Shot (`ctx`), Toolchains Sandbox-Safe e Segurança](guides/guia-replicacao-rag-e-seguranca.md) | Referência conceitual do kit: arquitetura, racional de cada ferramenta, fluxo decisório do guardrail (`allow`/`ask`/`deny`/`defer`) e diagramas. Para instalar, use o [`BOOTSTRAP.md`](BOOTSTRAP.md). |

---

## ✍️ Como Adicionar um Prompt ao Catálogo

1. Copie [`templates/prompt.md.example`](templates/prompt.md.example) para `prompts/<categoria>/<nome>.md`. O ID será `<categoria>/<nome>`; uma pasta nova vira uma categoria nova no catálogo.
2. Preencha o frontmatter:
   - `title`: nome curto exibido no catálogo;
   - `summary`: uma frase sobre o que o prompt muda no agente;
   - `harnesses`: lista de harnesses testados;
   - `covered_by_kit`: `true` só se o kit já gerar essas instruções.
3. Escreva a instrução **dentro do bloco** ```` ```markdown ```` da seção `## Snippet de Prompt / Instrução de Sistema`. Só esse bloco é instalado nos projetos; o resto do arquivo é documentação.
4. Atualize o catálogo e este README: `uv run ctx prompt catalog`.
5. Valide: `uv run python -m unittest discover -s tests`. Os testes falham se algum prompt estiver sem frontmatter ou snippet, se o catálogo/README estiver desatualizado ou se algum documento citar outra versão.
6. Para publicar: suba a versão em `pyproject.toml` e `src/ctx/__init__.py`, rode o passo 4 de novo, ajuste o tag em `BOOTSTRAP.md`, `prompts/INSTALL.md` e `llms.txt`, faça o commit e crie o tag (ex.: `v0.3.0`).

<details>
<summary>📁 Estrutura do repositório</summary>

```text
agent-harness-hub/
├── README.md                    # Catálogo (seções geradas por `ctx prompt catalog`)
├── prompts/
│   ├── <categoria>/<nome>.md    # Prompts (frontmatter + snippet)
│   ├── catalog.json             # Catálogo gerado
│   └── INSTALL.md               # Instruções para o agente instalar prompts
├── BOOTSTRAP.md                 # Instruções para o agente instalar o kit completo
├── llms.txt                     # Índice para LLMs (llmstxt.org)
├── guides/                      # Guias arquiteturais
├── templates/                   # prompt.md.example e policy.toml.example
├── src/ctx/                     # Motor ctx: init, prompt, status, search, map, bins, rule, doctor
│   └── kit/                     # Arquivos instalados pelo `ctx init`
├── scripts/                     # guardrail.py (hook PreToolUse) e venv-python (Python sempre do .venv)
├── tests/                       # Testes do motor, do kit e do catálogo
├── policy.toml                  # Política deste repositório
└── AGENTS.md / CLAUDE.md / GEMINI.md, .claude/, .agents/   # Gerados pelo kit para este repositório
```

</details>
