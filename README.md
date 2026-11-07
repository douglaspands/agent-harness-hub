# Agent Harness Hub 🎯

Repositório central de **boas práticas, arquiteturas, diretrizes de governança e prompts de alta eficiência** para uso em **Agent Harnesses** modernos (Google Antigravity, Claude Code, Aider, Swarm e frameworks afins).

O objetivo deste hub é catalogar soluções testadas que eliminam desperdício de tokens, previnem alucinações operacionais, garantem autonomia segura e elevam a precisão dos agentes de IA desde o **Turno 0 e Turno 1**.

---

## 📁 Estrutura do Repositório

O repositório foi organizado de forma modular para permitir a adição contínua de novos prompts, diretrizes e guias técnicos:

```text
agent-harness-hub/
├── README.md                                    # Índice mestre, objetivos e catálogo de recursos
├── guides/                                      # Guias arquiteturais aprofundados e manuais de replicação
│   └── guia-replicacao-rag-e-seguranca.md       # RAG local (ctx), Cache de Binários, Toolchains e Segurança
├── prompts/                                     # Snippets e instruções prontas para copiar e usar
│   ├── discovery/                               # Busca cirúrgica, redução de contexto e exploração rápida
│   │   └── descoberta-em-1-turno.md
│   ├── toolchains-and-executors/                # Resolução de executores (uv, asdf) e cache de binários
│   │   └── enforcement-toolchains-canonicos.md
│   ├── graph-engineering/                       # DAGs de subagentes, Fan-Out paralelo e consolidação Fan-In
│   │   └── dag-fanout-fanin.md
│   └── governance-and-guardrails/               # Políticas de segurança, containers efêmeros e Btrfs No-COW
│       └── container-first-e-btrfs.md
└── templates/                                   # Modelos e arquivos declarativos prontos para replicação
    └── policy.toml.example                      # Matriz de governança multi-harness (allow, ask, deny, toolchains)
```

---

## 📚 Catálogo de Recursos, Dicas e Prompts

Abaixo está a lista de materiais disponíveis no hub com seus respectivos significados, problemas resolvidos e compatibilidade:

### 1. Guias Arquiteturais e de Replicação (`guides/`)

| Guia | Harnesses | Foco Principal | Significado e O que Resolve |
| :--- | :--- | :--- | :--- |
| [**Guia de Replicação: RAG Local Ultra-Performático (`ctx`), Cache de Binários e Segurança**](guides/guia-replicacao-rag-e-seguranca.md) | Antigravity, Claude Code, CLI | Arquitetura Completa de Autonomia | Manual agnóstico e completo de replicação do motor `ctx` (SQLite FTS5/BM25 + AST chunker + `policy.toml` + Guardrail universal). Elimina a exploração cega de arquivos, sondagem repetitiva de binários (`which`), uso de executores errados e encadeamentos frágeis no terminal em menos de 2 minutos de bootstrap. |

---

### 2. Prompts e Diretrizes Prontas (`prompts/`)

| Prompt / Diretriz | Categoria | Harnesses | Significado e Benefício Técnico |
| :--- | :--- | :--- | :--- |
| [**Descoberta Cirúrgica em 1 Turno**](prompts/discovery/descoberta-em-1-turno.md) | `discovery` | Antigravity, Claude Code, Aider | **Elimina varreduras cegas de diretórios**: Instrui o agente a consultar o índice local (`ctx map` e `ctx search`) no Turno 1 em `< 10ms`, lendo fatias de 150 a 400 tokens em vez de despender 4 a 8 turnos lendo arquivos inteiros. |
| [**Enforcement de Toolchains Canônicos**](prompts/toolchains-and-executors/enforcement-toolchains-canonicos.md) | `toolchains` | Antigravity, Claude Code | **Acerto de runtime na 1ª tentativa (1-shot)**: Bloqueia atalhos errados (chamar `python3` global ou `/usr/bin/node`) e impede sondagens de binários (`which`/`whereis`), forçando os executores canônicos (`uv run`, `asdf exec`). |
| [**Graph Engineering (DAG Fan-Out/Fan-In)**](prompts/graph-engineering/dag-fanout-fanin.md) | `workflows` | Antigravity, Claude Code, Swarm | **Paralelismo estruturado e seguro**: Proíbe operadores de encadeamento opacos no shell (`&&`, `||`, `;`, `&`) e modela o fluxo de trabalho em nós DAG independentes com um agente consolidador final. |
| [**Container-First e Btrfs No-COW**](prompts/governance-and-guardrails/container-first-e-btrfs.md) | `governance` | Antigravity, Claude Code, Linux | **Proteção do sistema host e disco**: Prioriza execução de serviços auxiliares via containers efêmeros rootless (`podman`/`docker`) e preserva a integridade de SSDs em sistemas Linux Btrfs sugerindo `~/data` (`chattr +C` / No-COW). |

---

### 3. Modelos Declarativos (`templates/`)

| Arquivo | Descrição |
| :--- | :--- |
| [**`policy.toml.example`**](templates/policy.toml.example) | Modelo declarativo de governança unificada. Define listas de comandos (`allow`, `ask`, `deny`), filtros de pipes seguros, proteção de arquivos sensíveis, toolchains canônicos e regras de containers. |

---

## 🚀 Como Aplicar em um Novo Projeto

1. **Adicionar ao Prompt de Sistema ou Arquivo de Instruções**:
   - Incorpore os snippets de [`prompts/`](prompts/) no arquivo de contexto do seu agente (ex: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` ou `.cursorrules`).
2. **Replicar a Arquitetura Completa**:
   - Siga o passo a passo descrito no [Guia de Replicação](guides/guia-replicacao-rag-e-seguranca.md) para habilitar o RAG SQLite FTS5 local, o hook universal de guardrail e a sincronização multi-harness.
3. **Customizar a Política Declarativa**:
   - Utilize [`templates/policy.toml.example`](templates/policy.toml.example) como base para personalizar os comandos permitidos e toolchains do repositório.

---

## ✍️ Como Adicionar Novos Prompts e Dicas

Ao adicionar novos itens ao repositório:
1. **Escolha a pasta correta**:
   - `guides/`: Para arquiteturas de ponta a ponta, manuais e documentações conceituais.
   - `prompts/<categoria>/`: Para prompts focados, regras sistêmicas ou personas.
   - `templates/`: Para arquivos de configuração e boilerplates.
2. **Estruture o arquivo Markdown com**:
   - **Título e Objetivo Claro**
   - **Harnesses Compatíveis**
   - **Snippet de Prompt / Instrução Pronta para Uso**
   - **Significado Técnico e Impacto Observado**
3. **Atualize o índice deste `README.md`** adicionando o novo item à tabela correspondente.
