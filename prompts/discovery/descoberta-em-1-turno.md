---
title: Descoberta One-Shot em 1 Turno
summary: Troca varreduras cegas (ls -R, find, grep -r) por ctx status/map/search com preview de 25 linhas no Turno 1.
harnesses: [Claude Code, Antigravity, Codex, Cursor, Aider]
covered_by_kit: true
requires: [kit]
---

# Prompt: Descoberta One-Shot em 1 Turno (Anti-Exploração Cega)

## Objetivo
Evitar que agentes de IA desperdicem entre 4 e 8 turnos rodando `ls`, `find`, `grep` ou lendo arquivos inteiros na tentativa de descobrir a estrutura e o estado do projeto, e eliminar re-execuções para "ler o resto" de uma função ou adicionar flags esquecidas.

## Harnesses Compatíveis
- Google Antigravity (AGY / Antigravity IDE)
- Claude Code
- Aider / Roo Code / Cursor / Cline
- Qualquer agente com ferramentas de terminal (`run_command` / `Bash`)

## Snippet de Prompt / Instrução de Sistema

```markdown
### Diretriz de Descoberta One-Shot e Economia de Contexto
- NUNCA execute varreduras cegas (`ls -R`, `find .`, `tree` sem `-L`, `grep -r`) nem leia arquivos inteiros para localizar código ou documentação.
- No Turno 1, use o motor local (`.venv/bin/ctx` ou `uv run ctx`), em chamadas paralelas no mesmo turno:
  1. Estado consolidado (Git + changes OpenSpec com progresso X/Y + saúde do índice): `.venv/bin/ctx status`.
  2. Mapa estrutural do código ativo (sem `openspec/changes/archive/`): `.venv/bin/ctx map [subcaminhos...]`.
  3. Vários conceitos de uma vez, com 25 linhas de preview: `.venv/bin/ctx search "<q1>" "<q2>" --lines 25 [--path src]`.
- Use o retorno `arquivo:inicio-fim` para leituras cirúrgicas só quando o preview não bastar.
- Fora do `ctx`, use as Receitas One-Shot: `rg -n --heading -C 2 "<padrao>" [caminho]`, `fd --max-depth 3 --exclude archive "<padrao>"`, `tree -L 2 --dirsfirst`, `git log --oneline --decorate -n 10`.
```

## Significado e Impacto
- **Redução de Turnos**: Estado do Git + tasks + localização exata de múltiplos conceitos em 1 a 3 chamadas paralelas (< 15ms cada).
- **Economia de Tokens**: Substitui dumps de 3.000 a 15.000 tokens por recortes de 25 linhas focados.
- **Zero Re-execução**: O preview de 25 linhas e as Receitas One-Shot eliminam a 2ª chamada para "ler o resto" ou adicionar flags.
