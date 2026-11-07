# Prompt: Enforcement de Toolchains Canônicos e Cache de Binários

## Objetivo
Eliminar turnos de sondagem com `which`/`whereis` e impedir que agentes utilizem atalhos incorretos de runtime (como chamar `python3` global do sistema ou `/usr/bin/node` desatualizado).

## Harnesses Compatíveis
- Google Antigravity (AGY)
- Claude Code
- Ambientes com políticas declarativas (`policy.toml`) e hooks de pré-execução

## Snippet de Prompt / Instrução de Sistema

```markdown
### Executores Canônicos e Resolução de Ferramentas
- NÃO execute `which`, `whereis` ou `--version` para verificar a presença de ferramentas. Consulte o mapa de ferramentas pré-resolvidas em `.ctx/bins.json` ou na tabela injetada neste arquivo.
- Respeite estritamente os executores canônicos de cada linguagem declarada no projeto:
  - **Python**: Invoque EXCLUSIVAMENTE via `uv run python ...` ou `uv run <comando>`. É proibido invocar `python`, `python3` ou caminhos diretos de `.venv/bin/`.
  - **Node.js**: Invoque EXCLUSIVAMENTE via shims gerenciados (`asdf exec <comando>` ou shims em `~/.asdf/shims`). É proibido invocar `/usr/bin/node` ou `/usr/bin/npm` diretamente.
- Qualquer tentativa de utilizar binários diretos não canônicos será rejeitada imediatamente pelos guardrails de segurança.
```

## Significado e Impacto
- **Zero Turnos Desperdiçados**: Reduz 1 a 3 turnos de checagem no início de cada sessão.
- **Isolamento de Ambiente**: Impede a contaminação do ambiente host ou do Python do sistema.
- **Previsibilidade**: O agente acerta o comando correto na primeira tentativa (1-shot accuracy).
