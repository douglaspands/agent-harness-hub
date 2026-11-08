"""Testes do `ctx metrics` (leituras/escritas de arquivo via shell vs ferramentas nativas)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ctx.metrics import analyze, transcripts_dir_for


def tool_use(name: str, **inp: str) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


def line(content: list, ts: str = "2026-11-08T10:00:00Z") -> str:
    return json.dumps({"timestamp": ts, "message": {"content": content}})


class TestMetrics(unittest.TestCase):
    def test_counts_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "s1.jsonl").write_text(
                "\n".join(
                    [
                        line([tool_use("Read", file_path="a.py"), tool_use("Bash", command="cat b.py")]),
                        line([tool_use("Bash", command="sed -i 's/a/b/' c.py"), tool_use("Edit", file_path="d.py")]),
                        line([tool_use("Bash", command="git status")]),
                        line([{"type": "tool_result", "content": [{"type": "text", "text": "Leitura ... (file_tools=coach). Use Read"}]}]),
                        "linha inválida",
                    ]
                ),
                encoding="utf-8",
            )
            (d / "vazia.jsonl").write_text(line([tool_use("Bash", command="git status")]), encoding="utf-8")

            res = analyze(d)
            self.assertEqual(len(res["sessions"]), 1)
            t = res["totals"]
            self.assertEqual((t["native_read"], t["bash_read"], t["native_write"], t["bash_write"], t["coach_denies"]), (1, 1, 1, 1, 1))
            self.assertEqual(t["bash_read_pct"], 50.0)
            self.assertEqual(res["sessions"][0]["started"], "2026-11-08 10:00")

    def test_transcripts_dir_naming(self) -> None:
        d = transcripts_dir_for(Path("/home/u/Work/my.repo"))
        self.assertEqual(d.name, "-home-u-Work-my-repo")
        self.assertEqual(d.parent, Path.home() / ".claude" / "projects")


if __name__ == "__main__":
    unittest.main()
