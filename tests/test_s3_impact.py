"""STEP 3 (Phase 4) — impact spread: levels, grade filter, box grouping, CLI (hand-derived answers)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.query import Index

HERE = Path(__file__).resolve().parent
E = "fn:fixpkg.core.Engine"


class Step3Impact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = extract(HERE / "fixtures", "fixpkg", extra_dirs={})
        g.add_node("concept:util", "concept", "util")
        g.add_node("concept:core", "concept", "core")
        g.add_edge("concept:util", "mod:fixpkg.util", "maps", static="confirmed")
        g.add_edge("concept:core", "mod:fixpkg.core", "maps", static="confirmed")
        cls.data = g.to_dict()
        cls.ix = Index(cls.data)

    def test_s3_1_levels(self):
        r = self.ix.impact("fn:fixpkg.util.helper")
        self.assertEqual(r["levels"], [
            ["mod:fixpkg.core", "mod:fixpkg.sub.deep", "fn:fixpkg.sub.deep.deep_fn", "fn:fixpkg.core.plain",
             "fn:fixpkg.util.Worker.step", "fn:fixpkg.core.top"],
            [f"{E}.step.inner", f"{E}.run"],
            [f"{E}.aio", f"{E}.step"],
        ])
        self.assertEqual(self.ix.impact("fn:fixpkg.util.helper", max_level=1)["levels"], [r["levels"][0]])

    def test_s3_2_grade_filter(self):
        guess = self.ix.impact("data:data/events.jsonl", "guess")
        self.assertEqual(guess["levels"], [[f"{E}.__init__", f"{E}.load", f"{E}.save"]])
        self.assertEqual(self.ix.impact("data:data/events.jsonl", "static")["levels"], [])
        self.assertEqual(self.ix.impact("data:data/events.jsonl", "verified")["levels"], [])

    def test_s3_3_box_rollup_and_grouping(self):
        r = self.ix.impact("concept:util")
        self.assertEqual(r["levels"][0], ["mod:fixpkg.core", "mod:fixpkg.sub.deep", "fn:fixpkg.sub.deep.deep_fn",
                                          "fn:fixpkg.core.plain", "fn:fixpkg.core.top"])
        self.assertEqual(r["boxes"], {"concept:core": 1, "concept:util": 0})
        self.assertEqual(r["tests"], [])

    def test_s3_4_cycle_terminates(self):
        from minimap.graph import Graph
        g = Graph()
        for n in ("fn:a", "fn:b"):
            g.add_node(n, "function", n[3:])
        g.add_edge("fn:a", "fn:b", "calls", static="confirmed")
        g.add_edge("fn:b", "fn:a", "calls", static="confirmed")
        self.assertEqual(Index(g.to_dict()).impact("fn:a")["levels"], [["fn:b"]])

    def test_s3_5_cli(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "g.json"
            p.write_text(json.dumps(self.data), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
            run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(p), *a],
                                            capture_output=True, text=True, encoding="utf-8", env=env)
            j = run("--node", "fn:fixpkg.util.helper", "--impact", "--json")
            self.assertEqual(j.returncode, 0, j.stderr)
            self.assertEqual([len(x) for x in json.loads(j.stdout)["levels"]], [6, 2, 2])
            txt = run("--node", "concept:util", "--impact").stdout
            self.assertIn("L1 (5)", txt)
            self.assertIn("boxes: concept:core L1, concept:util L0", txt)
            self.assertIn("(edges >= guess)", txt)  # --impact default floor (STEP 8 split the CLI defaults)


if __name__ == "__main__":
    unittest.main()
