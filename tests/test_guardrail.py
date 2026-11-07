"""Testes unitários para o hook PreToolUse de segurança e governança."""

from __future__ import annotations

import unittest
from scripts.guardrail import (
    evaluate_file_access,
    evaluate_shell_command,
    format_response,
    has_chaining_operators,
    is_forbidden_toolchain,
)

SAMPLE_POLICY = {
    "commands": {
        "allow": ["git status", "git diff", "uv run", "./bin/ctx"],
        "ask": ["git push", "rm"],
        "deny": ["rm -rf /", "mkfs", "curl | bash"],
        "safe_pipe_filters": ["head", "tail", "rg", "grep", "wc"],
    },
    "paths": {
        "deny": ["~/.ssh", ".env", "/etc/shadow"],
        "protected": ["policy.toml", "scripts/guardrail.py"],
        "readonly_system": ["/etc/os-release"],
    },
    "toolchains": {
        "python": {
            "canonical_runner": "uv run",
            "forbidden_direct": ["python", "python3", "/usr/bin/python3"],
            "reason": "Use uv run.",
        },
        "nodejs": {
            "canonical_runner": "asdf exec",
            "forbidden_direct": ["/usr/bin/node", "/usr/bin/npm"],
            "reason": "Use asdf.",
        },
    },
}


class TestGuardrail(unittest.TestCase):
    def test_has_chaining_operators(self) -> None:
        self.assertTrue(has_chaining_operators("git status && git diff"))
        self.assertTrue(has_chaining_operators("ls -la ; echo done"))
        self.assertTrue(has_chaining_operators("sleep 5 &"))
        self.assertTrue(has_chaining_operators("cmd1 || cmd2"))
        self.assertTrue(has_chaining_operators("echo $(whoami)"))
        self.assertTrue(has_chaining_operators("echo `id`"))
        self.assertTrue(has_chaining_operators("line1\nline2"))
        self.assertFalse(has_chaining_operators("git status"))
        self.assertFalse(has_chaining_operators("uv run python -m unittest"))
        self.assertFalse(has_chaining_operators("git status | rg modified"))

    def test_forbidden_toolchains(self) -> None:
        # python3 direto deve ser proibido
        is_f, _ = is_forbidden_toolchain("python3 script.py", SAMPLE_POLICY)
        self.assertTrue(is_f)

        # /usr/bin/python3 direto deve ser proibido
        is_f2, _ = is_forbidden_toolchain("/usr/bin/python3 script.py", SAMPLE_POLICY)
        self.assertTrue(is_f2)

        # uv run python deve ser permitido
        is_f3, _ = is_forbidden_toolchain("uv run python script.py", SAMPLE_POLICY)
        self.assertFalse(is_f3)

    def test_evaluate_shell_command(self) -> None:
        # Comando permitido na whitelist
        dec, _ = evaluate_shell_command("git status", SAMPLE_POLICY)
        self.assertEqual(dec, "allow")

        # Comando proibido em deny
        dec_deny, _ = evaluate_shell_command("rm -rf /", SAMPLE_POLICY)
        self.assertEqual(dec_deny, "deny")

        # Encadeamento shell
        dec_chain, _ = evaluate_shell_command("git status && git diff", SAMPLE_POLICY)
        self.assertEqual(dec_chain, "deny")

        # Pipe seguro
        dec_pipe_ok, _ = evaluate_shell_command("git status | rg modified", SAMPLE_POLICY)
        self.assertEqual(dec_pipe_ok, "allow")

        # Pipe inseguro
        dec_pipe_ask, _ = evaluate_shell_command("git status | bash", SAMPLE_POLICY)
        self.assertEqual(dec_pipe_ask, "ask")

        # Comando em ask
        dec_ask, _ = evaluate_shell_command("git push origin main", SAMPLE_POLICY)
        self.assertEqual(dec_ask, "ask")

    def test_evaluate_file_access(self) -> None:
        # Acesso negado a credenciais
        dec_ssh, _ = evaluate_file_access("read_file", "~/.ssh/id_rsa", SAMPLE_POLICY)
        self.assertEqual(dec_ssh, "deny")

        dec_env, _ = evaluate_file_access("read_file", ".env", SAMPLE_POLICY)
        self.assertEqual(dec_env, "deny")

        # Escrita em arquivo protegido
        dec_prot, _ = evaluate_file_access("edit_file", "policy.toml", SAMPLE_POLICY)
        self.assertEqual(dec_prot, "ask")

    def test_format_response_protocols(self) -> None:
        # Formato Antigravity AGY
        agy_resp = format_response(is_antigravity=True, decision="allow", reason="OK")
        self.assertIn("decision", agy_resp)
        self.assertEqual(agy_resp["decision"], "allow")

        agy_ask = format_response(is_antigravity=True, decision="ask", reason="Confirmar")
        self.assertEqual(agy_ask["decision"], "force_ask")

        # Formato Claude Code
        claude_resp = format_response(is_antigravity=False, decision="allow", reason="OK")
        self.assertIn("hookSpecificOutput", claude_resp)
        self.assertEqual(claude_resp["hookSpecificOutput"]["permissionDecision"], "allow")


if __name__ == "__main__":
    unittest.main()
