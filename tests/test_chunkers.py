"""Cortes estruturais de C (tree-sitter, opcional) e Device Tree (M-03)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ctx.chunkers import MAX_CHUNK_LINES, c_parser, extract_dts_chunks
from ctx.rag import Chunk, chunk_file, search_many

DTS = """/dts-v1/;
#include "px30.dtsi"

/ {
\tmodel = "R36S";

\tbacklight: backlight {
\t\tcompatible = "pwm-backlight";
\t\tpwms = <&pwm1 0 25000 0>;  /* { não conta */
\t};

\tvcc_sd: vcc-sd {
\t\tregulator-name = "vcc_sd";
\t};
};

&dsi {
\tstatus = "okay";
\tpanel@0 {
\t\treg = <0>;
\t};
};
"""

C_SRC = """#include <linux/clk.h>
#define LANE_MAX 4

static const struct hstt hstt_table[] = {
\t{ 80, 21 },
\t{ 90, 23 },
};

/* Calcula a taxa por lane. */
static int dw_mipi_dsi_get_lane_mbps(void *priv, int lanes)
{
\tint mpclk = 1000;
\treturn mpclk * 24 / lanes;
}
"""


class TestDts(unittest.TestCase):
    def test_nodes_become_titled_chunks(self) -> None:
        chunks = extract_dts_chunks(Path("board.dts"), "board.dts", DTS, Chunk)
        titles = {c.title: c for c in chunks}
        self.assertIn("backlight: backlight", titles)
        self.assertIn("vcc_sd: vcc-sd", titles)
        self.assertIn("&dsi", titles)
        self.assertEqual(titles["backlight: backlight"].chunk_type, "dts_node")
        self.assertEqual((titles["backlight: backlight"].start_line, titles["backlight: backlight"].end_line), (7, 10))
        self.assertIn("panel@0", titles["&dsi"].content)  # nó filho fica dentro do pai

    def test_long_node_is_split_in_parts(self) -> None:
        body = "\n".join(f"\tprop{i} = <{i}>;" for i in range(MAX_CHUNK_LINES + 10))
        chunks = extract_dts_chunks(Path("x.dtsi"), "x.dtsi", f"&big {{\n{body}\n}};\n", Chunk)
        self.assertEqual([c.title for c in chunks if c.chunk_type == "dts_node"], ["&big (part 1)", "&big (part 2)"])


@unittest.skipUnless(c_parser(), "extra opcional `c` (tree-sitter-c) não instalado")
class TestC(unittest.TestCase):
    def test_definitions_become_titled_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "dsi.c"
            f.write_text(C_SRC, encoding="utf-8")
            chunks = chunk_file(f, Path(tmp))
        by_title = {c.title: c for c in chunks}
        fn = by_title["dw_mipi_dsi_get_lane_mbps"]
        self.assertEqual(fn.chunk_type, "c_function")
        self.assertIn("Calcula a taxa", fn.content)  # o comentário logo acima entra no trecho
        self.assertEqual(by_title["hstt_table"].chunk_type, "c_data")
        self.assertTrue(any("LANE_MAX" in t for t in by_title))  # #define no título do trecho de intervalo

    def test_search_brings_the_whole_function_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ctx").mkdir()
            (root / "dsi.c").write_text(C_SRC, encoding="utf-8")
            top = search_many(["get_lane_mbps"], workspace_root=root)["queries"][0]["results"][0]
        self.assertEqual(top["title"], "dw_mipi_dsi_get_lane_mbps")


class TestCFallback(unittest.TestCase):
    def test_without_tree_sitter_c_uses_generic_chunks(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp, mock.patch("ctx.chunkers.c_parser", return_value=None):
            f = Path(tmp) / "dsi.c"
            f.write_text(C_SRC, encoding="utf-8")
            chunks = chunk_file(f, Path(tmp))
        self.assertEqual([c.title for c in chunks], ["dsi.c (full)"])


if __name__ == "__main__":
    unittest.main()
