# Prompt: Graph Engineering (DAG Fan-Out + Consolidador Fan-In)

## Objetivo
Eliminar encadeamentos frágeis e perigosos no shell (`cmd1 && cmd2 &`) e estruturar fluxos de trabalho paralelos de forma nativa e auditável no harness de IA.

## Harnesses Compatíveis
- Google Antigravity (AGY / `invoke_subagent` / chamadas concorrentes de ferramentas)
- Claude Code (`Task` / `Agent`)
- Frameworks de Orquestração Multi-Agente (Swarm, LangGraph, AutoGen)

## Snippet de Prompt / Instrução de Sistema

```markdown
### Paralelismo Seguro e Graph Engineering
- É estritamente proibido encadear comandos shell usando operadores de junção (`&&`, `||`, `;`, `&`, subshells `$()` ou backticks). Cada comando deve ser atômico e verificável.
- Quando a tarefa exigir paralelismo, utilize Graph Engineering conforme a complexidade:
  1. **Paralelismo Leve (Mesmo Turno)**: Emita múltiplas chamadas atômicas de ferramentas independentes no mesmo turno. O harness cuidará da execução simultânea.
  2. **Paralelismo Complexo (Subagent DAG)**:
     - **Nó Orquestrador**: Mapeia o escopo e particiona tarefas independentes em pacotes isolados de arquivos.
     - **Nós de Fan-Out (Subagentes)**: Dispare subagentes especializados concorrentes com contexto limpo e escopo atômico.
     - **Nó Consolidador (Fan-In)**: Recebe reativamente os resultados de todos os nós, reconcilia as alterações e executa a bateria final de testes e validação.
```

## Significado e Impacto
- **Segurança de Execução**: Bloqueia injeções ou falhas em pipelines shell opacos.
- **Auditoria e Logs Claros**: Cada comando atômico gera um registro individual de saída e código de retorno.
- **Escalabilidade**: Tarefas de dezenas de arquivos podem ser paralelizadas em subagentes sem degradação do contexto.
