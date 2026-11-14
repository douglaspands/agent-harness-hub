# Changelog

## 0.5.0

Melhorias prototipadas e medidas num projeto real que usa o `ctx` como banco de testes (código bilíngue, C do
kernel, Device Tree, patches, operação por `ssh`). Os números M-NN são os da lista de melhorias desse projeto.
Decisões de design ainda não implementadas: [`docs/design/ctx-propostas.md`](docs/design/ctx-propostas.md).

### Busca
- **M-01** Recuo da consulta: todos os termos → substring (trigramas) → qualquer termo, com o modo no cabeçalho do resultado.
- **M-02** Pesos por coluna no BM25 (título 6, nome do arquivo 4, caminho 2, conteúdo 1, tipo 0,5).
- **M-03** Corte estrutural de C/H por definição (tree-sitter, extra `c`) e de DTS/DTSI por nó (sem dependência), com o símbolo no título.
- **M-04** Tabela FTS5 de trigramas (`chunks_tri`) para pedaços de identificador. Índice no esquema 4 (o 3 foi do protótipo; as colunas também são conferidas), recriado sozinho na primeira chamada.
- **M-05** Busca semântica local híbrida (extra `semantic`, model2vec `potion-base-8M`, RRF k=60, peso 0,8), pulada quando o BM25 já basta; modelo baixado só no `ctx index`, direto para `.ctx/models/`.
- **M-06** Prévia centrada na linha que casou; linhas cortadas em 200 caracteres.
- **M-11** `[search].glossary` (termos equivalentes do projeto) e palavras vazias PT/EN fora da consulta lexical.
- **M-13** Seções longas de markdown divididas por item de lista ou parágrafo, mantendo o título.
- **M-26** Nome do arquivo pesa no ranking (bônus quando um termo é o nome do arquivo); com `refresh = "incremental"`, resultados desatualizados são reindexados na hora.

### Guardrail
- **M-18** `paths.readonly_system` com `~` e `/**`, avaliado antes da regra do `~/data`; novo `paths.readonly_commands`.
- **M-19** Id de regra em cada bloqueio (`[regra: <id>]`), gravado no `.ctx/events.jsonl`; mensagem de encadeamento que diz o que fazer no lugar.
- **M-24** Padrões proibidos casam com o argv: `rm -rf /storage/x` e `rg "mksquashfs"` deixam de ser barrados; `rm -rf /`, `mkfs.*` e `curl | sh` continuam.
- **M-25** Script citado de `ssh host '…'`, `sh -c '…'` e `podman/docker run … sh -c '…'` passa só pelas regras destrutivas; em aspas duplas, `$(…)` (que roda localmente) continua recusado.

### Receitas e avaliação
- **M-07** Receitas declaram `requires = [...]`; `ctx recipe list` mostra o que falta no `commands.allow` e o `ctx init` libera os comandos da `copy-verify`.
- **M-19** `ctx evaluate` mostra os bloqueios por regra e quantos se repetiram na mesma sessão.
- **M-20** Veredito "provisório" abaixo de `[metrics.evaluate].confident_sessions` (padrão 30).
- **M-21** Sem fatos registrados, o `evaluate` recomenda `ctx fact add` com exemplos.
- **M-22** Adoção da busca por sessão, além de por chamada.

### Pacote
- Extras opcionais `c`, `semantic` e `all`; sem eles o `ctx` funciona como antes e o `ctx index` avisa uma vez o que ficou desligado.
- `ctx upgrade` acrescenta as dicas comentadas das chaves novas (`search-quality`, `paths-readonly`, `metrics-confidence`) também a projetos que já têm essas tabelas.
