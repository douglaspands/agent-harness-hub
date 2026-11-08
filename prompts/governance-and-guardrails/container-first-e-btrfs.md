---
title: Container-First e Btrfs No-COW
summary: Serviços e utilitários em containers efêmeros rootless; nada instalado no host sem confirmação; pergunta antes de usar ~/data (No-COW).
harnesses: [Claude Code, Antigravity, Codex, Cursor]
covered_by_kit: true
---

# Prompt: Política Container-First e Armazenamento Btrfs No-COW

## Objetivo
Proteger a máquina host contra instalações avulsas de pacotes e garantir máxima performance de escrita em bancos de dados pesados e volumes evitando fragmentação Copy-on-Write (Btrfs).

## Harnesses Compatíveis
- Google Antigravity (AGY)
- Claude Code
- Ambientes Linux (Fedora, Arch, openSUSE, etc.)

## Snippet de Prompt / Instrução de Sistema

```markdown
### Governança Container-First e I/O de Alta Performance
1. **Container-First**:
   - Para rodar serviços auxiliares, bancos de dados temporários (PostgreSQL, Redis, Qdrant) ou utilitários que não estejam no ambiente virtual local, utilize PREFERENCIALMENTE containers efêmeros rootless (`podman run --rm` ou `docker run --rm`).
   - NUNCA instale pacotes diretamente no sistema host (`dnf install`, `apt install`) sem confirmação explícita do usuário.
2. **Armazenamento de Alta Performance (Btrfs No-COW)**:
   - Caso a tarefa envolva criar ou processar bancos de dados pesados, datasets volumosos ou imagens de VM/containers sujeitos a escrita aleatória intensa, pergunte ao usuário se autoriza armazenar os dados em `~/data` (diretório com `chattr +C` / No-COW para evitar write amplification no Btrfs).
```

## Significado e Impacto
- **Host Limpo e Reprodutível**: Sem resíduos de dependências globais ou daemons desnecessários rodando no sistema.
- **Prevenção de Degradação de Disco**: Evita perda drástica de performance de I/O e desgaste desnecessário de SSDs em sistemas com sistema de arquivos Btrfs.
- **Transparência**: O usuário mantém controle total sobre o que toca o sistema fora do workspace.
