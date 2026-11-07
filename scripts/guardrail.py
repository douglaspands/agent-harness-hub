#!/usr/bin/env python3
"""Hook universal PreToolUse de alta performance (< 5ms) para governança e segurança.

Compatível com:
- Google Antigravity (AGY / .agents/hooks.json)
- Claude Code (.claude/settings.json)

100% biblioteca padrão do Python 3.11+ (zero dependências externas).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = WORKSPACE_ROOT / "policy.toml"


def load_policy() -> dict[str, Any]:
    """Carrega as regras do policy.toml com cache em memória."""
    if not POLICY_PATH.exists():
        return {}
    try:
        with open(POLICY_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def format_response(is_antigravity: bool, decision: str, reason: str) -> dict[str, Any]:
    """Formata a resposta estrita para o protocolo do harness correspondente."""
    if is_antigravity:
        # AGY protojson: "allow", "force_ask", "deny"
        agy_decision = "force_ask" if decision == "ask" else decision
        return {
            "decision": agy_decision,
            "reason": reason,
        }
    else:
        # Claude Code hookSpecificOutput: "allow", "ask", "deny"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }


def has_chaining_operators(cmd: str) -> bool:
    """Verifica se há encadeamento não-atômico no shell (&&, ||, ;, &, subshells)."""
    # Proibir quebras de linha e subshells
    if "\n" in cmd or "\r" in cmd or "$(" in cmd or "`" in cmd:
        return True

    # Ignorar redirecionamentos padrão como 2>&1 ou &>
    # Mas bloquear operadores lógicos &&, ||, ; e background & isolado
    tokens = cmd.split()
    for t in tokens:
        if t in {"&&", "||", ";"}:
            return True
        if t == "&":
            return True
        if t.endswith(";") and len(t) > 1:
            return True

    # Regex para operadores fora de aspas simples/duplas
    if re.search(r"(?<!\\)(&&|\|\||;)", cmd):
        return True
    return False


def is_forbidden_toolchain(cmd: str, policy: dict[str, Any]) -> tuple[bool, str]:
    """Valida se o comando invoca diretamente binários proibidos em [toolchains]."""
    toolchains = policy.get("toolchains", {})
    cmd_stripped = cmd.strip()

    try:
        parts = shlex.split(cmd_stripped)
    except ValueError:
        parts = cmd_stripped.split()

    if not parts:
        return False, ""

    first_bin = parts[0]
    base_bin = Path(first_bin).name

    for stack_name, tc in toolchains.items():
        forbidden = set(tc.get("forbidden_direct", []))
        canonical = tc.get("canonical_runner", "")
        reason = tc.get("reason", f"Use o executor canônico '{canonical}'.")

        # Verifica correspondência exata do comando ou basename
        if first_bin in forbidden or base_bin in forbidden:
            # Se for o próprio runner canônico, não proíbe
            if cmd_stripped.startswith(canonical):
                continue
            return True, f"Uso direto de '{first_bin}' proibido para stack {stack_name}. {reason} Executor canônico: '{canonical}'."

    return False, ""


def evaluate_shell_command(cmd: str, policy: dict[str, Any]) -> tuple[str, str]:
    """Aplica a matriz de decisão de segurança para comandos de terminal."""
    cmd_clean = cmd.strip()
    commands_cfg = policy.get("commands", {})

    # 1. Comandos proibidos expressamente (deny)
    deny_patterns = commands_cfg.get("deny", [])
    for pat in deny_patterns:
        if pat in cmd_clean:
            return "deny", f"Comando bloqueado pela política de segurança (padrão proibido: '{pat}')."

    # 2. Bloqueio estrito de encadeamento de comandos
    if has_chaining_operators(cmd_clean):
        return (
            "deny",
            "Encadeamento de comandos shell (&&, ||, ;, &, subshells) é proibido. "
            "Execute comandos atômicos ou utilize Graph Engineering (DAG de subagentes).",
        )

    # 3. Enforcement de Toolchains Canônicos
    is_forbidden, tc_reason = is_forbidden_toolchain(cmd_clean, policy)
    if is_forbidden:
        return "deny", tc_reason

    # 4. Checagem de paths negados em argumentos (~/.ssh, .env)
    deny_paths = policy.get("paths", {}).get("deny", [])
    for dp in deny_paths:
        expanded_dp = os.path.expanduser(dp)
        if dp in cmd_clean or expanded_dp in cmd_clean:
            return "deny", f"Acesso ao caminho sensível restrito '{dp}' é proibido."

    # 5. Checagem de ~/data (Btrfs No-COW)
    if "~/data" in cmd_clean or os.path.expanduser("~/data") in cmd_clean:
        return "ask", "Operação envolvendo ~/data (Btrfs No-COW) requer confirmação do usuário."

    # 6. Avaliação de Pipes Simples
    safe_filters = set(commands_cfg.get("safe_pipe_filters", []))
    if "|" in cmd_clean:
        segments = [s.strip() for s in cmd_clean.split("|")]
        first_seg = segments[0]
        # Valida primeiro segmento contra whitelist
        first_allowed = any(first_seg == a or first_seg.startswith(a + " ") for a in commands_cfg.get("allow", []))
        if not first_allowed:
            return "ask", f"Primeiro comando do pipe ('{first_seg}') não está na whitelist de execução autônoma."

        # Valida demais segmentos contra safe_pipe_filters
        for seg in segments[1:]:
            seg_bin = seg.split()[0] if seg.split() else ""
            if seg_bin not in safe_filters:
                return "ask", f"Filtro de pipe '{seg_bin}' requer confirmação do usuário."

        return "allow", "Pipe simples com filtros seguros aprovado automaticamente."

    # 7. Comandos em commands.allow (whitelist)
    allow_list = commands_cfg.get("allow", [])
    for a in allow_list:
        if cmd_clean == a or cmd_clean.startswith(a + " ") or cmd_clean.startswith(a + "\t"):
            return "allow", f"Comando aprovado na whitelist ('{a}')."

    # 8. Comandos em commands.ask
    ask_list = commands_cfg.get("ask", [])
    for ask_item in ask_list:
        if cmd_clean == ask_item or cmd_clean.startswith(ask_item + " "):
            return "ask", f"Comando '{ask_item}' requer confirmação explícita do usuário."

    # 9. Padrão para comandos fora da whitelist
    return "ask", "Comando fora da whitelist pré-aprovada requer confirmação do usuário."


def evaluate_file_access(tool_name: str, file_path_str: str, policy: dict[str, Any]) -> tuple[str, str]:
    """Aplica a matriz de decisão de segurança para leitura e escrita de arquivos."""
    paths_cfg = policy.get("paths", {})
    expanded_path = Path(os.path.expanduser(file_path_str)).resolve()

    # 1. Caminhos proibidos (deny)
    for dp in paths_cfg.get("deny", []):
        dp_exp = Path(os.path.expanduser(dp)).resolve()
        try:
            if expanded_path == dp_exp or dp_exp in expanded_path.parents:
                return "deny", f"Acesso ao arquivo/diretório confidencial '{dp}' é estritamente proibido."
        except Exception:
            pass

    # 2. Caminhos do Btrfs No-COW (~/data)
    btrfs_path = Path(os.path.expanduser("~/data")).resolve()
    try:
        if expanded_path == btrfs_path or btrfs_path in expanded_path.parents:
            return "ask", "Operações no volume ~/data (Btrfs No-COW) requerem confirmação do usuário."
    except Exception:
        pass

    # 3. Caminhos protegidos contra escrita desautorizada
    is_write = any(w in tool_name.lower() for w in ("write", "edit", "create", "delete", "remove"))
    if is_write:
        for prot in paths_cfg.get("protected", []):
            prot_exp = (WORKSPACE_ROOT / prot).resolve()
            if expanded_path == prot_exp:
                return "ask", f"Alteração no arquivo crítico de governança '{prot}' requer confirmação do usuário."

    # 4. Leitura em caminhos permitidos do sistema operacional
    is_read = any(r in tool_name.lower() for r in ("read", "view", "cat", "get"))
    if is_read:
        for ro_sys in paths_cfg.get("readonly_system", []):
            ro_exp = Path(ro_sys).resolve()
            try:
                if expanded_path == ro_exp or ro_exp in expanded_path.parents:
                    return "allow", f"Leitura segura em arquivo de sistema ('{ro_sys}')."
            except Exception:
                pass

    # 5. Fronteira do Workspace
    try:
        if expanded_path == WORKSPACE_ROOT or WORKSPACE_ROOT in expanded_path.parents:
            return "allow", "Operação dentro do escopo do repositório autorizada."
    except Exception:
        pass

    # Fora do workspace
    return "ask", f"Arquivo '{file_path_str}' fora da raiz do workspace requer confirmação do usuário."


def main() -> None:
    """Entry point do hook PreToolUse."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.stdout.write(json.dumps({"decision": "allow", "reason": "Entrada vazia"}))
            return
        payload = json.loads(raw_input)
    except Exception as e:
        sys.stdout.write(json.dumps({"decision": "allow", "reason": f"Fallback JSON: {e}"}))
        return

    # Auto-detecção de Harness
    is_antigravity = "tool_name" in payload or "name" in payload or "CommandLine" in str(payload)
    if "hook_event_name" in payload and payload.get("hook_event_name") == "PreToolUse":
        is_antigravity = False

    tool_name = payload.get("tool_name") or payload.get("name") or ""
    tool_args = payload.get("tool_args") or payload.get("tool_input") or payload.get("args") or {}

    policy = load_policy()

    # Identificar se é comando shell
    cmd_str = ""
    if isinstance(tool_args, dict):
        cmd_str = tool_args.get("CommandLine") or tool_args.get("command") or tool_args.get("cmd") or ""
    elif isinstance(tool_args, str):
        cmd_str = tool_args

    if cmd_str or any(sh_tool in tool_name.lower() for sh_tool in ("command", "bash", "sh", "terminal")):
        decision, reason = evaluate_shell_command(cmd_str, policy)
        out = format_response(is_antigravity, decision, reason)
        sys.stdout.write(json.dumps(out))
        return

    # Identificar se é operação em arquivo
    file_path = ""
    if isinstance(tool_args, dict):
        file_path = tool_args.get("Path") or tool_args.get("path") or tool_args.get("file_path") or ""
    if file_path:
        decision, reason = evaluate_file_access(tool_name, file_path, policy)
        out = format_response(is_antigravity, decision, reason)
        sys.stdout.write(json.dumps(out))
        return

    # Padrão seguro para ferramentas não mapeadas
    out = format_response(is_antigravity, "allow", "Ferramenta padrão autorizada.")
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
