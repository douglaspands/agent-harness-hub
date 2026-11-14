"""Testes multi-harness do guardrail (Claude Code, Codex, Cursor, Gemini CLI, Antigravity) e de [file_tools]."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.guardrail import (
    WORKSPACE_ROOT,
    classify_file_op,
    detect_harness,
    evaluate_patch,
    evaluate_shell_command,
    extract_request,
    file_tools_decision,
    format_response,
    patch_paths,
)

POLICY = {
    "commands": {"allow": ["git status", "rg", "cat", "ls"], "ask": ["git push"], "deny": ["rm -rf /"], "safe_pipe_filters": ["head", "rg"]},
    "paths": {"deny": ["~/.ssh", ".env"], "protected": ["policy.toml"], "readonly_system": []},
    "file_tools": {"mode": "coach", "harnesses": ["claude", "codex", "cursor", "gemini", "antigravity"]},
}
ROOT = str(WORKSPACE_ROOT)

PAYLOADS = {
    "claude": {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": ROOT},
    "codex": {"hook_event_name": "PreToolUse", "session_id": "s", "turn_id": "t", "model": "m", "tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": ROOT},
    "cursor": {"hook_event_name": "beforeShellExecution", "cursor_version": "2.5", "command": "git status", "cwd": ROOT, "workspace_roots": [ROOT]},
    "gemini": {"hook_event_name": "BeforeTool", "session_id": "s", "tool_name": "run_shell_command", "tool_input": {"command": "git status"}, "cwd": ROOT},
    "antigravity": {"tool_name": "run_command", "tool_args": {"CommandLine": "git status"}},
}


class TestDetection(unittest.TestCase):
    def test_detect_each_harness(self) -> None:
        for harness, payload in PAYLOADS.items():
            self.assertEqual(detect_harness(payload), harness)

    def test_extract_request(self) -> None:
        for harness, payload in PAYLOADS.items():
            kind, value, _ = extract_request(payload, harness)
            self.assertEqual((kind, value), ("shell", "git status"), harness)
        read = {"hook_event_name": "beforeReadFile", "cursor_version": "2.5", "file_path": f"{ROOT}/.env", "content": "X=1"}
        self.assertEqual(extract_request(read, "cursor")[:2], ("file", f"{ROOT}/.env"))
        gem_read = {"hook_event_name": "BeforeTool", "tool_name": "read_file", "tool_input": {"absolute_path": f"{ROOT}/README.md"}}
        self.assertEqual(extract_request(gem_read, "gemini")[:2], ("file", f"{ROOT}/README.md"))


class TestResponses(unittest.TestCase):
    def test_claude(self) -> None:
        self.assertEqual(format_response("claude", "deny", "x")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(format_response("claude", "ask", "x")["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(format_response("claude", "defer", "x"), {})

    def test_codex_has_no_ask(self) -> None:
        deny = format_response("codex", "ask", "git push requer confirmação.")
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("peça que ele execute ou aprove", deny["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(format_response("codex", "ask", "x", ask_fallback="allow"), {})
        self.assertEqual(format_response("codex", "allow", "x"), {})
        self.assertEqual(format_response("codex", "defer", "x"), {})

    def test_cursor(self) -> None:
        self.assertEqual(format_response("cursor", "allow", "x"), {"permission": "allow"})
        deny = format_response("cursor", "deny", "bloqueado")
        self.assertEqual((deny["permission"], deny["agent_message"], deny["user_message"]), ("deny", "bloqueado", "bloqueado"))
        self.assertEqual(format_response("cursor", "ask", "x")["permission"], "ask")
        self.assertEqual(format_response("cursor", "defer", "x")["permission"], "ask")

    def test_gemini(self) -> None:
        self.assertEqual(format_response("gemini", "allow", "x"), {"decision": "allow"})
        self.assertEqual(format_response("gemini", "deny", "motivo"), {"decision": "deny", "reason": "motivo"})
        self.assertEqual(format_response("gemini", "ask", "x")["decision"], "deny")
        self.assertEqual(format_response("gemini", "defer", "x"), {})

    def test_antigravity_and_legacy_bool(self) -> None:
        self.assertEqual(format_response("antigravity", "defer", "x")["decision"], "force_ask")
        self.assertEqual(format_response(True, "ask", "x")["decision"], "force_ask")
        self.assertIn("hookSpecificOutput", format_response(False, "allow", "x"))


class TestCodexApplyPatch(unittest.TestCase):
    PATCH = "*** Begin Patch\n*** Update File: {a}\n@@\n-x\n+y\n*** Add File: {b}\n+novo\n*** End Patch\n"

    def test_patch_paths(self) -> None:
        patch = "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n*** Delete File: c.py\n*** End Patch"
        self.assertEqual(patch_paths(patch), ["a.py", "b.py", "c.py"])

    def test_patch_decisions(self) -> None:
        inside = self.PATCH.format(a="src/app.py", b="tests/test_app.py")
        self.assertEqual(evaluate_patch(inside, POLICY, WORKSPACE_ROOT)[0], "allow")
        protected = self.PATCH.format(a="src/app.py", b="policy.toml")
        self.assertEqual(evaluate_patch(protected, POLICY, WORKSPACE_ROOT)[0], "ask")
        outside = self.PATCH.format(a="src/app.py", b="/tmp/fora/x.py")
        self.assertEqual(evaluate_patch(outside, POLICY, WORKSPACE_ROOT)[0], "ask")
        secret = self.PATCH.format(a=".env", b="src/x.py")
        self.assertEqual(evaluate_patch(secret, POLICY, WORKSPACE_ROOT)[0], "deny")
        self.assertEqual(evaluate_patch("sem cabeçalhos", POLICY, WORKSPACE_ROOT)[0], "defer")


class TestEndToEnd(unittest.TestCase):
    """Executa o guardrail como processo, exatamente como cada harness faz."""

    def run_hook(self, payload: dict) -> dict:
        out = subprocess.run(
            [sys.executable, str(WORKSPACE_ROOT / "scripts" / "guardrail.py")],
            input=json.dumps(payload), capture_output=True, text=True, check=True,
            env={**os.environ, "CTX_EVENTS_LOG": "0"},  # bloqueios de teste não entram nas métricas do repo
        )
        return json.loads(out.stdout)

    def test_codex_apply_patch_on_policy_is_denied(self) -> None:
        payload = dict(PAYLOADS["codex"], tool_name="apply_patch",
                       tool_input={"command": "*** Begin Patch\n*** Update File: policy.toml\n@@\n-a\n+b\n*** End Patch"})
        out = self.run_hook(payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cursor_read_secret_is_denied(self) -> None:
        out = self.run_hook({"hook_event_name": "beforeReadFile", "cursor_version": "2.5", "file_path": f"{ROOT}/.env", "content": ""})
        self.assertEqual(out["permission"], "deny")

    def test_gemini_chaining_is_denied(self) -> None:
        payload = dict(PAYLOADS["gemini"], tool_input={"command": "git status && rm x"})
        self.assertEqual(self.run_hook(payload)["decision"], "deny")


class TestFileTools(unittest.TestCase):
    def test_classify(self) -> None:
        reads = ["cat src/a.py", "head -n 40 README.md", "tail -20 x.log", "sed -n '10,40p' a.py", "cat a.py | rg foo", "nl a.py"]
        writes = ["sed -i 's/a/b/' a.py", "echo x > a.txt", "printf 'x' >> a.txt", "git diff | tee out.patch",
                  "cat > a.py <<'EOF'\nprint(1)\nEOF", "perl -pi -e 's/a/b/' a.py"]
        neither = ["git show HEAD:a.py | head -20", "tail -f app.log", "ls >/dev/null", "rg -n foo src", "git status 2>&1",
                   "cat", "sed 's/a/b/'", "git log -n 5 | head"]
        for c in reads:
            self.assertEqual(classify_file_op(c), "read", c)
        for c in writes:
            self.assertEqual(classify_file_op(c), "write", c)
        for c in neither:
            self.assertIsNone(classify_file_op(c), c)

    def test_coach_per_harness(self) -> None:
        self.assertIn("Read", file_tools_decision("cat a.py", POLICY, "claude")[1])
        self.assertIn("read_file", file_tools_decision("cat a.py", POLICY, "gemini")[1])
        self.assertIn("apply_patch", file_tools_decision("sed -i 's/a/b/' a.py", POLICY, "codex")[1])
        self.assertIsNone(file_tools_decision("cat a.py", POLICY, "codex"))  # Codex lê via shell por design

    def test_off_and_unlisted_harness(self) -> None:
        off = dict(POLICY, file_tools={"mode": "off"})
        self.assertIsNone(file_tools_decision("cat a.py", off, "claude"))
        only_claude = dict(POLICY, file_tools={"mode": "coach", "harnesses": ["claude"]})
        self.assertIsNone(file_tools_decision("cat a.py", only_claude, "cursor"))

    def test_shell_matrix_with_coach(self) -> None:
        self.assertEqual(evaluate_shell_command("cat src/a.py", POLICY)[0], "deny")
        heredoc = evaluate_shell_command("cat > a.py <<'EOF'\nx\nEOF", POLICY)
        self.assertEqual(heredoc[0], "deny")
        self.assertIn("Write", heredoc[1])  # orientação de ferramenta, não a mensagem genérica de encadeamento
        self.assertEqual(evaluate_shell_command("git status", POLICY)[0], "allow")
        self.assertEqual(evaluate_shell_command("cat ~/.ssh/id_rsa", POLICY)[1].startswith("Acesso"), True)


if __name__ == "__main__":
    unittest.main()
