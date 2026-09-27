"""Agent test follow-up (2026-09-27): the CLI states its limits where agents read the answer.

Found by giving fresh agents real tasks: the weakest model cited --impact exactly but missed that a changed event
format breaks the file's readers, and called a subprocess-tested CLI "untested" although it had quoted the limit."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.graph import Graph
from minimap.query import CHILD_PROCESS_NOTE
from tests.test_s8_hotspots import small_graph

HERE = Path(__file__).resolve().parent
E = "fn:fixpkg.core.Engine"


def cli(data: dict, *args):
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "g.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
        return subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(p), *args], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)


class AgentNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fix = extract(HERE / "fixtures", "fixpkg", extra_dirs={}).to_dict()

    def test_impact_names_the_files_written(self):
        out = cli(self.fix, "--node", f"{E}.save", "--impact").stdout  # save writes data/events.jsonl (core.py:48)
        self.assertIn("writes files: data:data/events.jsonl. Code that READS them is not in the list above", out)
        self.assertIn(f"  data:data/events.jsonl: 1 readers\n    {E}.load  fixpkg/core.py:45", out)  # listed, no extra step
        j = json.loads(cli(self.fix, "--node", "cls:fixpkg.core.Engine", "--impact", "--json").stdout)
        self.assertEqual(j["writes_files"], ["data:data/events.jsonl"])  # the class includes save
        self.assertEqual(j["file_readers"]["data:data/events.jsonl"]["readers"],
                         [{"id": f"{E}.load", "evidence": "fixpkg/core.py:45", "callers": []}])

    def test_indirect_readers_are_listed(self):
        # an agent test found a function that read the data file THROUGH another reader
        g = Graph()
        for n in ("w", "r", "c"):
            g.add_node(f"fn:{n}", "function", n)
        g.add_node("data:f.jsonl", "data", "f.jsonl")
        g.add_edge("fn:w", "data:f.jsonl", "writes", static="guess", evidence="x.py:1")
        g.add_edge("fn:r", "data:f.jsonl", "reads", static="guess", evidence="x.py:2")
        g.add_edge("fn:c", "fn:r", "calls", static="confirmed", evidence="x.py:3")
        out = cli(g.to_dict(), "--node", "fn:w", "--impact").stdout
        self.assertIn("    fn:r  x.py:2\n      read through it by: fn:c", out)
        self.assertNotIn("writes files", cli(self.fix, "--node", "fn:fixpkg.util.helper", "--impact").stdout)

    def test_child_process_entry_points_are_tagged(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "cpkg").mkdir()
            (root / "cpkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "cpkg" / "cli.py").write_text(
                "def main():\n    return 0\n\n\ndef helper():\n    return 1\n\n\n"
                "if __name__ == '__main__':\n    main()\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_c.py").write_text(
                "import subprocess, sys\n\n\ndef test_cli():\n"
                "    subprocess.run([sys.executable, '-m', 'cpkg.cli'], check=True)\n", encoding="utf-8")
            data = extract(root, "cpkg").to_dict()
            for n in data["nodes"]:
                if n["id"] == "fn:tests.test_c.test_cli":
                    n["ran"] = True  # traced graph: the test ran, main and helper did not
            out = cli(data, "--coverage").stdout  # no boxes here, so it is reported on the "outside boxes" line
            self.assertIn("also never ran but started as a child process: fn:cpkg.cli.main (tests/test_c.py:5)", out)
            self.assertNotIn("fn:cpkg.cli.helper (", out)  # not an entry point: no tag
            j = json.loads(cli(data, "--coverage", "--json").stdout)
            self.assertEqual(j["child_process_entry_points"], {"fn:cpkg.cli.main": ["tests/test_c.py:5"]})

    def test_never_ran_lists_carry_the_child_process_note(self):
        self.assertIn(CHILD_PROCESS_NOTE, cli(small_graph(), "--hotspots").stdout)
        self.assertIn(CHILD_PROCESS_NOTE, cli(small_graph(), "--coverage").stdout)  # b, c and K.m2 never ran
        quiet = small_graph()
        for n in quiet["nodes"]:
            n["ran"] = True
        self.assertNotIn(CHILD_PROCESS_NOTE, cli(quiet, "--coverage").stdout)

    def test_runtime_only_path_carries_a_warning(self):
        g = Graph()
        for n in ("a", "b", "c"):
            g.add_node(f"fn:{n}", "function", n)
        g.add_edge("fn:a", "fn:b", "calls", static="confirmed")
        g.add_edge("fn:b", "fn:c", "calls", runtime=True, evidence="x.py:3")
        data = g.to_dict()
        self.assertIn("note: a runtime-only step", cli(data, "--node", "fn:a", "--path", "fn:c").stdout)
        self.assertNotIn("note:", cli(data, "--node", "fn:a", "--path", "fn:b").stdout)


if __name__ == "__main__":
    unittest.main()
