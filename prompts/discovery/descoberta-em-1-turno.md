# Prompt: Descoberta Cirúrgica em 1 Turno (Anti-Exploração Cega)

## Objetivo
Evitar que agentes de IA desperdicem entre 4 e 8 turnos rodando `ls`, `find`, `grep` ou lendo arquivos inteiros na tentativa de descobrir a estrutura do projeto.

## Harnesses Compatíveis
- Google Antigravity (AGY / Antigravity IDE)
- Claude Code
- Aider / Roo Code / Cursor / Cline
- Qualquer agente com ferramentas de terminal (`run_command` / `Bash`)

## Snippet de Prompt / Instrução de Sistema

```markdown
### Diretriz de Descoberta Cirúrgica e Economia de Contexto
- NUNCA execute varreduras cegas de diretórios (`ls -R`, `find .`, `tree`) nem leia arquivos inteiros para localizar código ou documentação.
- No Turno 1, utilize prioritariamente o motor de busca local e o mapa estrutural:
  1. Para visão da arquitetura e das seções do repositório: execute `uv run ctx map` (ou ferramenta equivalente de mapa).
  2. Para localizar implementações, classes, funções ou regras específicas: execute `uv run ctx search "<termo>" --top 5`.
- Utilize o retorno `arquivo:inicio-fim` para realizar leituras cirúrgicas estritamente nas linhas necessárias (`read_file` com intervalo de linhas), consumindo apenas 150 a 400 tokens por busca.
```

## Significado e Impacto
- **Redução de Latência**: Localização exata da linha em < 10ms.
- **Economia de Tokens**: Substitui dumps de 3.000 a 15.000 tokens por recortes de 150 a 400 tokens.
- **Eliminação de Alucinação**: O modelo foca apenas no bloco relevante de código ou especificação.
