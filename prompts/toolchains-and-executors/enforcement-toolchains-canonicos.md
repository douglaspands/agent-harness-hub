---
title: Toolchains Sandbox-Safe e Coaching One-Shot
summary: Python sempre do .venv (sem uv, python3 só cria o venv), Node via asdf, zero sondagem de binários e receitas One-Shot.
harnesses: [Claude Code, Antigravity, Codex, Cursor]
covered_by_kit: true
requires: [kit]
---

# Prompt: Toolchains Sandbox-Safe, Cache de Binários e Coaching One-Shot

## Objetivo
Eliminar turnos de sondagem com `which`/`whereis`, impedir que agentes utilizem atalhos incorretos de runtime (como chamar `python3` global do sistema ou `/usr/bin/node` desatualizado) e evitar retries `exit 127` em sandboxes que não montam `~/.local/bin/uv`.

## Harnesses Compatíveis
- Google Antigravity (AGY)
- Claude Code
- Ambientes com políticas declarativas (`policy.toml`) e hooks de pré-execução

## Snippet de Prompt / Instrução de Sistema

```markdown
### Executores Canônicos e Resolução de Ferramentas
- NÃO execute `which`, `whereis` ou `--version` para verificar a presença de ferramentas. Consulte `.ctx/bins.json` ou a tabela injetada neste arquivo.
- Respeite os executores de cada linguagem declarada no projeto:
  - **Python**: `uv run python ...` / `uv run <comando>` ou os executores locais do workspace `.venv/bin/ctx` e `.venv/bin/python` (Sandbox-Safe: funcionam mesmo sem `uv` no PATH). É proibido invocar `python`, `python3` ou `/usr/bin/python*` globais.
  - **Node.js**: `asdf exec <comando>` ou shims em `~/.asdf/shims`. É proibido invocar `/usr/bin/node` ou `/usr/bin/npm` diretamente.
- Comandos ineficientes (`grep -r`, `find .`, `ls -R`, `tree` sem `-L`, `git log` sem `-n`) são recusados com a Receita One-Shot correta na mensagem: rode a receita indicada na próxima chamada.
- Binários globais não canônicos são rejeitados imediatamente (`deny`) pelo guardrail.
```

## Significado e Impacto
- **Zero Turnos Desperdiçados**: Reduz 1 a 3 turnos de checagem no início de cada sessão.
- **Zero `exit 127`**: Os executores do `.venv` acertam na 1ª tentativa dentro ou fora de sandbox.
- **Isolamento de Ambiente**: Impede a contaminação do ambiente host ou do Python do sistema.
- **Previsibilidade**: O agente acerta o comando correto na primeira tentativa (1-shot accuracy).
