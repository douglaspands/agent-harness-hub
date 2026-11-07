---
name: workspace-ctx
description: Aceleração cirúrgica de contexto em 1 turno (RAG local FTS5, cache de binários, toolchains canônicos e Graph Engineering). Ative no Turno 1 de qualquer tarefa no repositório.
---

# Workspace CTX: Motor de Aceleração e Governança

Esta skill instrui o agente sobre como operar com máxima eficiência de contexto e estrita conformidade com as regras de governança e toolchains do repositório.

## 1. Diretriz de Descoberta em 1 Turno (< 10ms)
- **Anti-Exploração Cega**: NUNCA execute comandos como `ls -R`, `find .`, `tree` ou leia arquivos inteiros para localizar código ou regras.
- No **Turno 1**, use sempre o motor RAG local:
  - Para entender a estrutura e divisões do projeto: execute `uv run ctx map` (ou `./bin/ctx map`).
  - Para localizar classes, funções, regras ou temas: execute `uv run ctx search "<termo>" --top 5` (ou `./bin/ctx search "<termo>"`).
- O resultado retornará referências cirúrgicas no formato `arquivo:inicio-fim`. Leia apenas esse intervalo de linhas (`read_file` com linhas específicas), consumindo apenas 150 a 400 tokens.

## 2. Ferramentas e Executores Canônicos
- **Zero Sondagem**: Não execute `which`, `whereis` ou `--version`. Consulte `.ctx/bins.json` ou execute `uv run ctx bins`.
- **Python**: Invoque EXCLUSIVAMENTE via `uv run` (ou `./bin/ctx`). É proibido invocar `python` ou `python3` diretamente.
- **Node.js**: Invoque EXCLUSIVAMENTE via `asdf exec` ou shims em `~/.asdf/shims`.

## 3. Graph Engineering (Proibição de Encadeamento Shell)
- Comandos com `&&`, `||`, `;`, `&`, subshells `$()` ou backticks serão rejeitados imediatamente (`deny`) pelo guardrail.
- **Paralelismo Leve**: Execute múltiplas chamadas atômicas de ferramentas no mesmo turno.
- **Paralelismo Complexo**: Estruture tarefas como DAG Fan-Out com subagentes especializados independentes e consolidação Fan-In.

## 4. Governança de Containers e Btrfs
- **Container-First**: Utilitários auxiliares e bancos de dados temporários devem rodar via containers efêmeros rootless (`podman run --rm` ou `docker run --rm`). NUNCA instale pacotes no host.
- **Armazenamento Btrfs**: Pergunte ao usuário antes de salvar bancos de dados ou datasets pesados em `~/data` (`chattr +C` / No-COW).
