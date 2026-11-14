# Propostas do `ctx` ainda não implementadas (decisão e custo)

Itens medidos no projeto de testes que ficaram só como desenho na 0.5.0. Cada um tem a decisão tomada e o custo
estimado, para virar issue ou entrar numa versão seguinte. Os números M-NN são os da lista de melhorias do projeto de
testes; o que foi levado está no [`CHANGELOG.md`](../../CHANGELOG.md).

## M-08 — `copy-verify` confere o disco, não o cache de páginas

**Problema.** O `sha256sum` logo depois do `sync`, na mesma montagem, pode ler o cache de páginas do kernel e não o
dispositivo; um leitor USB que corrompe gravações passa na conferência.

**Decisão.** Nova etapa opcional da receita embutida: com `--direct`, a leitura de conferência usa `O_DIRECT`
(`dd iflag=direct bs=1M | sha256sum`, ou leitura em Python com `os.posix_fadvise(..., POSIX_FADV_DONTNEED)` antes
de ler). Desmontar/montar só leitura fica fora do `ctx` (exige privilégio e conhece o dispositivo): a receita só
documenta o passo. `O_DIRECT` exige alinhamento e não funciona em todo sistema de arquivos; o fallback é o
`fadvise`, com aviso de que a conferência pode ter lido o cache.

**Custo.** Pequeno (uma etapa e uma opção na receita, mais testes com arquivo temporário). Risco: diferenças de
`O_DIRECT` entre sistemas de arquivos (tmpfs não aceita).

## M-09 — Referência fora do workspace com nome (`kernel:drivers/...`)

**Problema.** Árvores geradas pelo build mudam de nome a cada build (`linux-6.18.62-<hash>`); os resultados mostram
caminhos absolutos longos e o índice trata cada nome como arquivos novos.

**Decisão.** `[index.reference.<nome>] root = "~/data/projeto/src/linux-*"` (curinga resolvido para o diretório mais
recente) e `globs = [...]` relativos à raiz. O caminho guardado no índice é `<nome>:<relativo>`; a prévia e o
`Read` recebem o absoluto resolvido na hora. Trocar a raiz (hash novo) reindexa só os arquivos com mtime/tamanho
diferentes, comparando pelo caminho relativo.

**Custo.** Médio: mudança no `collect_files`, no formato do caminho guardado (esquema 4), no `read_preview` e no
`ctx map`; migração automática (o índice é cache).

## M-10 — Processo residente para a busca

**Problema.** Cada `ctx search` é um processo novo que importa numpy e carrega o modelo semântico: ~0,3 s com o
`potion-base-8M` e ~3,3 s com o multilíngue.

**Decisão.** Servidor opcional por projeto (socket Unix em `.ctx/search.sock`, iniciado pela primeira busca com a
semântica ligada e encerrado após 10 minutos parado), que mantém o modelo e a matriz de vetores em memória. O CLI
tenta o socket com timeout curto e, se não houver servidor, busca sozinho como hoje. O servidor só lê o índice
(mesma regra da busca) e recarrega a matriz quando o `mtime` do `index.db` muda.

**Custo.** Médio a alto: ciclo de vida do processo, concorrência, limpeza do socket órfão e testes. Só vale junto
com o modelo multilíngue (ver "Não levar") ou com índices muito grandes.

## M-12 — Ligações entre trechos (grafo de código)

**Problema.** Perguntas reais atravessam ligações (patch → arquivo → função; `compatible` do DTS →
`of_match_table`; `&dsi` → `dsi@ff450000`) e a busca devolve trechos soltos.

**Decisão.** Tabela `links (from_rowid, to_rowid, kind)` preenchida na indexação com o que os cortes já extraem
(símbolos dos patches, nomes de função C, rótulos e `compatible` do DTS), resolvida por nome depois de indexar
tudo. Interface: `ctx search --related` (acrescenta os vizinhos de 1º grau do 1º resultado) e `ctx refs <símbolo>`.

**Custo.** Médio: extração já existe em parte (`chunkers.py`, `extract_patch_chunks`); falta a resolução e a CLI.
Risco de ruído com nomes genéricos (`init`, `probe`): resolver só nomes com 2+ ocorrências distintas e prefixo do
arquivo.

## M-14 — Classificar o `rg` antes de contar como desvio

**Problema.** O `evaluate` conta todo `rg`/`grep`/`sed` como desvio do `ctx search`, inclusive busca exata num
arquivo conhecido e busca fora do índice (logs no cartão, árvore gerada pelo build).

**Decisão.** Em `metrics.is_detour`, classificar pelo argv: **alvo único** (último posicional é um arquivo),
**fora do índice** (caminho fora do workspace ou ignorado pelo índice) e **busca ampla** (diretório do workspace ou
sem caminho). Só a busca ampla conta como desvio; o relatório mostra as três contagens.

**Custo.** Pequeno: só `metrics.py`/`evaluate.py` e testes. Usa o mesmo filtro de escopo do índice
(`_path_excluded`) sem abrir o banco.

## M-15 — Bloco de instruções curto

**Problema.** O bloco gerado no `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` custa ≈ 1.700 tokens por sessão, mais que o
ganho medido; boa parte repete o que o guardrail já ensina no momento do bloqueio.

**Decisão.** Bloco curto (Turno 1: `status`/`map`/`search`; `recall` em sessão retomada; "o guardrail explica o
resto, inclusive o comando certo") e o detalhe na skill `workspace-ctx`, carregada só quando usada. Opção
`[ctx].instructions = "short" | "full"`, com `short` como padrão numa versão seguinte, depois de medir. O `evaluate`
passa a medir o custo pelo bloco real de cada harness ativo (hoje usa o maior).

**Custo.** Pequeno no código (`policy.py`); o custo real é a medição antes/depois em sessões reais, porque menos
instrução pode aumentar os bloqueios.

## M-16 + M-27 — `upstream get` sem declarar o projeto, e busca sem caminho conhecido

**Problema.** O `ctx upstream get` exige `[upstream.<nome>]` no `policy.toml` antes do primeiro uso, então o agente
baixa pela web; e não dá para achar um arquivo upstream sem saber o caminho (precisou de `git clone`).

**Decisão.**
- `ctx upstream get github:<dono>/<repo>@<ref> <caminho>`: sem declaração, monta a URL de
  `raw.githubusercontent.com`; o cache usa `<dono>--<repo>/<ref>`. Com `ref` que não é tag nem sha (ramo), o
  manifesto guarda o sha resolvido pela API e o `list` avisa que o conteúdo é o do dia do download.
- `ctx upstream grep github:<dono>/<repo>@<ref> <padrão>`: clone raso (`git clone --depth 1 --filter=blob:none`)
  em `.ctx/upstream/<...>/repo`, `rg` nele e indexação só dos arquivos que casaram (`kind = upstream`).
- O `evaluate` lista URLs buscadas mais de uma vez e sugere o bloco `[upstream.<nome>]` pronto.

**Custo.** Médio: rede só por comando explícito (regra atual mantida), validação do `<dono>/<repo>`, testes sem
rede (servidor HTTP local e repositório git local).

## M-17 — Receita que falha mostra o passo e o motivo no `evaluate`

**Problema.** O `evaluate` mostra "N execuções, 0 ok" sem dizer por quê.

**Decisão.** O evento `recipe run` passa a gravar `failed_step`, `decision` (deny/ask/defer) ou `exit`, e o
id de regra do guardrail quando foi bloqueio. O `evaluate` agrupa por receita e motivo e sugere remover receitas sem
sucesso em N sessões. A 0.5.0 já mostra os `requires` que faltam (M-07), a causa mais comum.

**Custo.** Pequeno: `cli.cmd_recipe`, `metrics.event_summary`, `evaluate` e testes.

## M-21 (resto) — Sugerir `ctx fact add` no momento da descoberta

**Problema.** O agente não registra fatos sozinho; o `evaluate` só recomenda depois.

**Decisão.** Dica (nunca bloqueio) no guardrail, como a de receitas: quando a sessão faz um `ctx search` e depois 3+
leituras do mesmo arquivo, ou um comando falha e o mesmo comando com outros argumentos passa, o hook acrescenta como
contexto adicional "isso custou caro: registre com `ctx fact add ... --source arquivo:linha`". Histórico curto em
`.ctx/hint-history.json` (já existe). A captura de fim de sessão (`ctx fact review`) continua como rede de segurança.

**Custo.** Pequeno a médio: o guardrail só vê comandos shell; leituras pela ferramenta `Read` chegam ao hook como
evento de arquivo, então o histórico precisa registrar os dois tipos.

## Não levar (por enquanto)

- **Modelo multilíngue como padrão** (`potion-multilingual-128M`): ~480 MB e 3,3 s de carga por chamada. O glossário
  (M-11) resolveu boa parte das perguntas em português; reavaliar só com o processo residente (M-10).
- **M-23** (coach das ferramentas de arquivo ligado por padrão): ainda em medição no projeto de testes; o padrão
  continua `[file_tools] mode = "off"`.
