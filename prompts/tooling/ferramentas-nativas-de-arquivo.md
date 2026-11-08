---
title: Ferramentas Nativas de Arquivo
summary: Faz o agente ler, editar e criar arquivos com as ferramentas do harness em vez de cat/head/sed -i/heredoc/redirecionamento no shell.
harnesses: [Claude Code, Antigravity, Codex, Cursor]
covered_by_kit: true
requires: []
---

# Prompt: Ferramentas Nativas de Arquivo (sem leitura/escrita via shell)

## Objetivo
Evitar que o agente use o shell para ler (`cat`, `head`, `tail`, `sed -n`) ou escrever arquivos (`sed -i`, `>`, `>>`, heredoc, `tee`). Leituras via shell despejam arquivos inteiros no contexto e geram prompts de permissão; escritas via shell perdem a verificação de trecho exato das ferramentas de edição e escapam das checagens de caminho do harness.

## Harnesses Compatíveis
- Claude Code (`Read`, `Edit`, `Write`)
- Google Antigravity (ferramentas nativas de arquivo)
- Codex e outros harnesses só com shell (regra condicional: trechos delimitados + mecanismo de patch)

## Snippet de Prompt / Instrução de Sistema

```markdown
### Ferramentas Nativas de Arquivo
- Se o seu harness tem ferramentas nativas de arquivo (ex.: `Read`/`Edit`/`Write` no Claude Code), use-as para ler, editar e criar arquivos. Para ler vários arquivos, faça várias chamadas de leitura no mesmo turno; para trechos, use offset/limit.
- Não use o shell para ler (`cat`, `head`, `tail`, `sed -n`) nem para escrever arquivos (`sed -i`, `>`, `>>`, heredoc `<<EOF`, `tee`). O shell fica para executar programas, testes, git e buscas (`rg`).
- Se o harness só tem shell (ex.: Codex), leia trechos delimitados (`sed -n 'A,Bp' arquivo`, `rg -n -C 2`) em vez de arquivos inteiros e aplique mudanças pelo mecanismo de patch do harness (ex.: `apply_patch`), não por redirecionamento.
```

## Significado e Impacto
- **Menos contexto desperdiçado**: leituras por intervalo em vez de arquivos inteiros.
- **Menos prompts de permissão**: ferramentas de arquivo seguem as regras de permissão do harness para o projeto.
- **Edições verificáveis**: a substituição exata falha se o trecho não existir, em vez de corromper o arquivo.
- **Com o kit**: o guardrail pode reforçar a regra (`[file_tools] mode = "coach"`) e o `ctx metrics` mede a adesão.
