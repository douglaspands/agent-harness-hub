---
name: workspace-ctx
description: Autonomia com segurança no repositório (ctx status/map/search One-Shot, índice saudável, memória recall/fact, upstream em cache, receitas de várias etapas, cache de binários, toolchains Sandbox-Safe, comandos atômicos e ferramentas nativas de arquivo). Ative no Turno 1 de qualquer tarefa no repositório.
---
<!-- CTX_SKILL_START (bloco gerenciado pelo `ctx upgrade`: edite fora destes marcadores) -->

# Workspace CTX: Motor de Aceleração e Governança

Esta skill instrui o agente a operar com máxima eficiência de contexto, acertando cada comando na 1ª tentativa e respeitando as regras de governança do repositório (`policy.toml` + `scripts/guardrail.py`).

## 1. Descoberta One-Shot em 1 Turno (< 15ms)
- **Anti-Exploração Cega**: NUNCA execute `ls -R`, `find .`, `tree` sem `-L`, `grep -r` ou leia arquivos inteiros para localizar código ou regras. O guardrail recusa essas variantes (`deny`) e devolve a Receita One-Shot correta.
- No **Turno 1**, use o motor `ctx` (`.venv/bin/ctx` ou `uv run ctx`), em chamadas paralelas no mesmo turno quando fizer sentido:
  - `.venv/bin/ctx status` — Git (`status -s -b`), changes OpenSpec ativas com progresso `X/Y` e próximas tarefas, saúde do índice (tamanho, arquivos, idade, escopo) e do cache.
  - `.venv/bin/ctx map [subcaminhos...]` — mapa estrutural (oculta `openspec/changes/archive/`; use `--include-archive` se precisar).
  - `.venv/bin/ctx search "<q1>" "<q2>" --lines 25 [--path src]` — várias queries numa chamada, com 25 linhas de preview por resultado.
- O retorno `arquivo:inicio-fim` indica exatamente o que ler; leia só esse intervalo quando o preview não bastar.

## 2. Ferramentas, Toolchains Sandbox-Safe e Receitas One-Shot
- **Zero Sondagem**: Não execute `which`, `whereis` ou `--version`. Consulte `.ctx/bins.json` ou `.venv/bin/ctx bins`.
- **Python**: sempre a partir do `.venv` do projeto — `uv run ...` ou os executores locais `.venv/bin/ctx` / `.venv/bin/python` (funcionam mesmo em sandbox sem `uv`). `python`, `python3` e `/usr/bin/python*` globais recebem `deny`.
  - **Sem `uv` na máquina**: use o Python da máquina **apenas** para criar o venv (`python3 -m venv .venv`) e, a partir daí, só `.venv/bin/python` (ex.: `.venv/bin/python -m pip install ...`). Nunca rode scripts ou testes com o Python global.
- **Node.js**: `asdf exec` ou shims em `~/.asdf/shims`. `/usr/bin/node` e afins recebem `deny`.
- **Receitas One-Shot**: `rg -n --heading -C 2 "<padrao>" [caminho]`, `fd --max-depth 3 --exclude archive "<padrao>"`, `tree -L 2 --dirsfirst -I ".venv|__pycache__|.git|.ctx|archive"`, `git status -s -b`, `git log --oneline --decorate -n 10`.

## 3. Decisões do Guardrail
- `deny`: segredos, padrões destrutivos, encadeamento shell, toolchains globais, comandos ineficientes (com a receita correta na mensagem).
- `ask`: `sudo`, instalação de pacotes, `systemctl start/stop/restart`, `git push`/`commit`, wrappers (`env`, `xargs`, `sh -c`…), `>`, `tee`, `--privileged`, escrita em arquivos protegidos ou fora do workspace, `~/data`, `ctx recipe run ... --yes`.
- `defer`: baixo risco fora da whitelist — o harness decide pelo próprio modo de permissão.

## 4. Comandos Atômicos (sem encadeamento shell)
- Comandos com `&&`, `||`, `;`, `&`, subshells `$()` ou backticks são rejeitados (`deny`) pelo guardrail.
- Execute um comando por chamada, para que cada um seja avaliado e auditado sozinho. Rotina de várias etapas que se repete? Use uma receita (seção 7).

## 5. Ferramentas Nativas de Arquivo
- Leia, edite e crie arquivos com as ferramentas do harness (Claude Code: `Read`/`Edit`/`Write`; Gemini CLI: `read_file`/`replace`/`write_file`; Codex: `apply_patch` para escrever), não com `cat`/`head`/`sed -n` nem `sed -i`/`>`/heredoc/`tee`.
- Com `[file_tools] mode = "coach"` no `policy.toml`, o guardrail nega essas variantes com a ferramenta certa na mensagem. A adesão é medida com `.venv/bin/ctx metrics`.

## 6. Governança de Containers e Btrfs
- **Container-First**: Utilitários auxiliares e bancos de dados temporários devem rodar via containers efêmeros rootless (`podman run --rm` ou `docker run --rm`). NUNCA instale pacotes no host sem confirmação.
- **Armazenamento Btrfs**: Pergunte ao usuário antes de salvar bancos de dados ou datasets pesados em `~/data` (`chattr +C` / No-COW).

## 7. Índice saudável, memória, upstream e receitas
- **Busca nunca trava**: `ctx search` não escreve no índice e para em 3 s (`--timeout`); ao estourar devolve o `rg` equivalente pronto — rode-o. Avisos `⚠ ... mudaram desde a indexação` pedem `ctx index`. Se algo parecer lento ou estranho: `ctx doctor`, e se falhar `ctx doctor --fix` (checkpoint do WAL, VACUUM, reindex).
- **Escopo**: o índice respeita `.gitignore`, ignora binários, arquivos grandes e diretórios de build/cache/venv. Material de referência fora do git entra por `[index.reference]` no `policy.toml`; filtre com `ctx search --scope code|reference|upstream`. Arquivos `.patch`/`.diff` são indexados com arquivos tocados, símbolos adicionados e hunks.
- **Sessão retomada ou contexto compactado?** Antes de reabrir arquivos ou repetir comandos, consulte a memória local: `ctx recall "<pergunta>"` (ex.: `"último comando de build que deu certo"`, `"URL usada para o patch X"`) e `ctx fact search "<termo>"`. Nada sai da máquina.
- **Registre o que custou caro descobrir**: `ctx fact add "<fato>" --source arquivo:linha|url --tags a,b` (valores medidos, decisões, armadilhas). `ctx fact stale` lista fatos cuja fonte mudou depois. Candidatos automáticos do fim da sessão: `ctx fact review`.
- **Fontes upstream**: `ctx upstream get <projeto> <versão> <caminho>` baixa uma única vez para `.ctx/upstream/<projeto>/<versão>/<caminho>`, indexa e devolve o caminho local — leia com `Read`; não baixe de novo à mão. Projetos e URLs ficam em `[upstream.*]` no `policy.toml`.
- **Rotinas de várias etapas**: `ctx recipe list` mostra as receitas; `ctx recipe run <nome> [args]` executa todas as etapas numa chamada (cada etapa passa pelo guardrail e a saída volta resumida). Exemplo: `ctx recipe run copy-verify <origem> <destino>`.
- **Medição**: `ctx metrics` mostra, por sessão, buscas do ctx vs desvios para `rg`/`grep`/`sed`, latência, chamadas economizadas por receita e acertos do recall.
- **Está compensando?** Se o usuário pedir uma avaliação de eficiência/utilidade do ctx (ex.: "avalie a eficiência do ctx", `/ctx-avaliar`), rode `ctx evaluate` e resuma o veredito por recurso, o ganho líquido estimado (tokens são estimativas) e a recomendação — sem inventar números.
<!-- CTX_SKILL_END -->
