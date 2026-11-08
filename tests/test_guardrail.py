"""Testes unitários para o hook PreToolUse de segurança e governança."""

from __future__ import annotations

import os
import unittest

from scripts.guardrail import (
    WORKSPACE_ROOT,
    evaluate_file_access,
    evaluate_shell_command,
    format_response,
    has_chaining_operators,
    is_forbidden_toolchain,
)

SAMPLE_POLICY = {
    "commands": {
        "allow": ["git status", "git diff", "git log", "uv run", "./bin/ctx", ".venv/bin/python", ".venv/bin/ctx", "rg", "cat", "tree -L", "ls"],
        "ask": ["git push", "rm", "dnf install"],
        "deny": ["rm -rf /", "mkfs", "curl | bash"],
        "safe_pipe_filters": ["head", "tail", "rg", "grep", "wc"],
        "inefficient_variants": {
            "grep -r": "Use rg -n --heading -C 2",
            "find .": "Use fd --max-depth 3",
            "ls -R": "Use tree -L 2",
            "tree_unbounded": "Passe -L ao tree",
            "git_log_unbounded": "Use git log -n 10",
        },
    },
    "paths": {
        "deny": ["~/.ssh", ".env", "/etc/shadow"],
        "protected": ["policy.toml", "scripts/guardrail.py"],
        "readonly_system": ["/etc/os-release"],
    },
    "toolchains": {
        "python": {
            "canonical_runner": "uv run",
            "sandbox_runners": [".venv/bin/ctx", ".venv/bin/python"],
            "bootstrap_allowed": ["python3 -m venv .venv"],
            "forbidden_direct": ["python", "python3", "/usr/bin/python3"],
            "reason": "Use uv run.",
        },
        "nodejs": {
            "canonical_runner": "asdf exec",
            "forbidden_direct": ["/usr/bin/node", "/usr/bin/npm"],
            "reason": "Use asdf.",
        },
    },
    "storage": {"btrfs_nocow": {"path": "~/data"}},
}


def decide(cmd: str) -> str:
    return evaluate_shell_command(cmd, SAMPLE_POLICY)[0]


class TestChaining(unittest.TestCase):
    def test_has_chaining_operators(self) -> None:
        self.assertTrue(has_chaining_operators("git status && git diff"))
        self.assertTrue(has_chaining_operators("ls -la ; echo done"))
        self.assertTrue(has_chaining_operators("sleep 5 &"))
        self.assertTrue(has_chaining_operators("cmd1 || cmd2"))
        self.assertTrue(has_chaining_operators("echo $(whoami)"))
        self.assertTrue(has_chaining_operators("echo `id`"))
        self.assertTrue(has_chaining_operators("line1\nline2"))
        self.assertTrue(has_chaining_operators("( cd /tmp )"))
        self.assertFalse(has_chaining_operators("git status"))
        self.assertFalse(has_chaining_operators("uv run python -m unittest"))
        self.assertFalse(has_chaining_operators("git status | rg modified"))
        self.assertFalse(has_chaining_operators("git status 2>&1 | head -5"))

    def test_quoted_operators_are_not_split(self) -> None:
        self.assertEqual(decide('rg "a|b" src'), "allow")
        self.assertEqual(decide('rg -n "foo(" src'), "allow")


class TestToolchains(unittest.TestCase):
    def test_forbidden_globals(self) -> None:
        self.assertTrue(is_forbidden_toolchain("python3 script.py", SAMPLE_POLICY)[0])
        self.assertTrue(is_forbidden_toolchain("/usr/bin/python3 script.py", SAMPLE_POLICY)[0])
        self.assertFalse(is_forbidden_toolchain("uv run python script.py", SAMPLE_POLICY)[0])

    def test_sandbox_runners_allowed(self) -> None:
        self.assertFalse(is_forbidden_toolchain(".venv/bin/python -m unittest", SAMPLE_POLICY)[0])
        self.assertFalse(is_forbidden_toolchain("./.venv/bin/python -m unittest", SAMPLE_POLICY)[0])
        abs_ctx = str(WORKSPACE_ROOT / ".venv" / "bin" / "ctx")
        self.assertFalse(is_forbidden_toolchain(f"{abs_ctx} status", SAMPLE_POLICY)[0])
        self.assertEqual(decide(".venv/bin/python -m unittest discover -s tests"), "allow")
        self.assertEqual(decide(f"{abs_ctx} status"), "allow")

    def test_global_python_only_to_create_venv(self) -> None:
        # Sem uv: o python global pode APENAS criar o .venv do workspace
        self.assertFalse(is_forbidden_toolchain("python3 -m venv .venv", SAMPLE_POLICY)[0])
        self.assertNotEqual(decide("python3 -m venv .venv"), "deny")
        self.assertEqual(decide("python3 -m venv /tmp/outro"), "deny")
        self.assertEqual(decide("python3 -m pip install requests"), "deny")
        self.assertEqual(decide("python3 -m venv .venv-x"), "deny")
        # Depois, tudo pelo .venv
        self.assertEqual(decide(".venv/bin/python -m pip --version"), "allow")

    def test_forbidden_inside_pipe(self) -> None:
        self.assertEqual(decide("cat x.py | python3"), "deny")


class TestShellDecisions(unittest.TestCase):
    def test_basic_matrix(self) -> None:
        self.assertEqual(decide("git status"), "allow")
        self.assertEqual(decide("rm -rf /"), "deny")
        self.assertEqual(decide("git status && git diff"), "deny")
        self.assertEqual(decide("git status | rg modified"), "allow")
        self.assertEqual(decide("git push origin main"), "ask")

    def test_coaching_one_shot(self) -> None:
        for cmd in ("grep -rn foo src", "find . -name x", "ls -R", "tree", "git log"):
            dec, reason = evaluate_shell_command(cmd, SAMPLE_POLICY)
            self.assertEqual(dec, "deny", cmd)
            self.assertTrue(reason.startswith(("Use", "Passe")), reason)
        self.assertEqual(decide("tree -L 2"), "allow")
        self.assertEqual(decide("git log -n 5"), "allow")
        self.assertEqual(decide("git log --max-count=3"), "allow")
        self.assertEqual(decide("git log | head -20"), "allow")

    def test_strong_ask(self) -> None:
        self.assertEqual(decide("echo hi > out.txt"), "ask")
        self.assertEqual(decide("git status | tee log.txt"), "ask")
        self.assertEqual(decide("sudo dnf install foo"), "ask")
        self.assertEqual(decide("xargs rm"), "ask")
        self.assertEqual(decide("git status | bash"), "ask")
        self.assertEqual(decide("podman run --privileged alpine"), "ask")
        self.assertEqual(decide("podman run --rm -v ~/data:/data alpine"), "ask")
        self.assertEqual(decide(f"podman run --rm -v {os.path.expanduser('~/data')}:/data alpine"), "ask")

    def test_harmless_redirects(self) -> None:
        self.assertEqual(decide("ls >/dev/null"), "allow")
        self.assertEqual(decide("git status 2>&1"), "allow")

    def test_defer_low_risk(self) -> None:
        self.assertEqual(decide("make build"), "defer")
        self.assertEqual(decide("git status | sort"), "defer")
        self.assertEqual(decide("cat /etc/hosts"), "defer")
        self.assertEqual(decide('echo "unterminated'), "defer")
        self.assertEqual(decide(""), "defer")
        self.assertEqual(decide("cat /etc/os-release"), "allow")

    def test_secrets_denied(self) -> None:
        self.assertEqual(decide("cat ~/.ssh/id_rsa"), "deny")
        self.assertEqual(decide("cat src/.env"), "deny")
        self.assertEqual(decide("rg token .env"), "deny")


class TestFileAccess(unittest.TestCase):
    def test_deny_and_protected(self) -> None:
        self.assertEqual(evaluate_file_access("Read", "~/.ssh/id_rsa", SAMPLE_POLICY)[0], "deny")
        self.assertEqual(evaluate_file_access("Read", ".env", SAMPLE_POLICY)[0], "deny")
        self.assertEqual(evaluate_file_access("Read", "sub/dir/.env", SAMPLE_POLICY)[0], "deny")
        self.assertEqual(evaluate_file_access("Edit", "policy.toml", SAMPLE_POLICY)[0], "ask")
        self.assertEqual(evaluate_file_access("Read", "policy.toml", SAMPLE_POLICY)[0], "allow")

    def test_harness_dirs_and_external(self) -> None:
        uid = os.getuid()
        self.assertEqual(evaluate_file_access("Write", f"/tmp/claude-{uid}/proj/scratch/a.md", SAMPLE_POLICY)[0], "allow")
        self.assertEqual(evaluate_file_access("Write", "~/.claude/projects/x/memory/m.md", SAMPLE_POLICY)[0], "allow")
        self.assertEqual(evaluate_file_access("Write", "/tmp/other/file.txt", SAMPLE_POLICY)[0], "ask")
        self.assertEqual(evaluate_file_access("Read", "/tmp/other/file.txt", SAMPLE_POLICY)[0], "defer")
        self.assertEqual(evaluate_file_access("Read", "~/data/db.sqlite", SAMPLE_POLICY)[0], "ask")


class TestProtocols(unittest.TestCase):
    def test_format_response_protocols(self) -> None:
        self.assertEqual(format_response(True, "allow", "OK")["decision"], "allow")
        self.assertEqual(format_response(True, "ask", "?")["decision"], "force_ask")
        self.assertEqual(format_response(True, "defer", "?")["decision"], "force_ask")

        claude = format_response(False, "allow", "OK")
        self.assertEqual(claude["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(format_response(False, "defer", "baixo risco"), {})


if __name__ == "__main__":
    unittest.main()
