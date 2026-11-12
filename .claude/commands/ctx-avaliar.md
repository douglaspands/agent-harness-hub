---
description: Avalia se o ctx está compensando (chamadas e tokens economizados vs custo) e recomenda manter, ajustar ou desligar cada recurso
argument-hint: "[--since 30d] [--model <nome>]"
---
Avalie a eficiência do `ctx` neste projeto para eu decidir se está valendo o esforço.

1. Rode `.venv/bin/ctx evaluate $ARGUMENTS` (um comando, sem encadeamento). Se o comando não existir, o ctx está desatualizado: rode `.venv/bin/ctx upgrade` e repita.
2. Resuma o resultado em português, nesta ordem:
   - **Veredito geral** em uma frase (compensa / marginal / não compensa / sem dados) e o **ganho líquido estimado por sessão** (ganho menos o custo das instruções do próprio ctx).
   - **Por recurso** (busca e índice, recall e fatos, upstream em cache, receitas, coaching do guardrail): veredito + a evidência numérica que o sustenta, em uma linha cada.
   - **Por modelo**, se o relatório trouxer a quebra: onde o ctx ajuda mais e onde atrapalha.
   - **Recomendação**: o que manter, o que ajustar (com o ajuste concreto no `policy.toml`) e o que desligar.
3. Seja fiel ao relatório: os tokens são **estimativas** (chars/4) e devem ser rotulados assim. Se a amostra for pequena ("sem dados"), diga isso em vez de concluir. Não invente números nem arredonde a favor do ctx.
