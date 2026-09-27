"""V1 Graph model units (DESIGN.md §7)."""
import tempfile
import unittest
from pathlib import Path

from minimap.graph import Graph, grade


class V1(unittest.TestCase):
    def test_v1_1_ids_unique_and_well_formed(self):
        g = Graph()
        g.add_node("fn:a.b", "function", "b")
        with self.assertRaises(ValueError):
            g.add_node("bad id", "function", "x")
        with self.assertRaises(ValueError):
            g.add_node("fn:a.c", "nonsense", "c")
        with self.assertRaises(ValueError):
            g.add_node("fn:a.b", "class", "b")  # same id, different kind
        g.add_node("fn:a.b", "function", "b", line=3)
        self.assertEqual(len(g.nodes), 1)
        self.assertEqual(g.nodes["fn:a.b"]["line"], 3)

    def test_v1_2_edge_merge(self):
        g = Graph()
        g.add_edge("fn:a", "fn:b", "calls", static="guess", evidence="a.py:5", candidates=["fn:b"])
        g.add_edge("fn:a", "fn:b", "calls", static="confirmed", evidence="a.py:3")
        g.add_edge("fn:a", "fn:b", "calls", runtime=True, evidence="a.py:3")
        g.add_edge("fn:a", "fn:b", "imports", static="confirmed", evidence="a.py:1")
        self.assertEqual(len(g.edges), 2)
        e = g.edges[("fn:a", "fn:b", "calls")]
        g.finalize()
        self.assertEqual(e["static"], "confirmed")
        self.assertTrue(e["runtime"])
        self.assertEqual(e["evidence"], ["a.py:3", "a.py:5"])
        self.assertEqual(e["grade"], "verified")
        g2 = Graph()
        g2.add_edge("fn:a", "fn:b", "calls", static="confirmed")
        g2.add_edge("fn:a", "fn:b", "calls", static="guess")  # guess never downgrades confirmed
        self.assertEqual(g2.edges[("fn:a", "fn:b", "calls")]["static"], "confirmed")

    def test_v1_3_grade_table(self):
        self.assertEqual(grade("confirmed", True), "verified")
        self.assertEqual(grade("guess", True), "verified")
        self.assertEqual(grade("confirmed", False), "static")
        self.assertEqual(grade("guess", False), "guess")
        self.assertEqual(grade(None, True), "runtime-only")
        with self.assertRaises(ValueError):
            grade(None, False)

    def test_v1_4_round_trip_no_bom(self):
        g = Graph("D:/x", "pkg")
        g.add_node("mod:pkg.a", "module", "a", file="pkg/a.py", line=1)
        g.add_node("fn:pkg.a.f", "function", "f", file="pkg/a.py", line=2, end=3)
        g.add_edge("mod:pkg.a", "fn:pkg.a.f", "contains", static="confirmed")
        g.sources["pkg/a.py"] = "0" * 64
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "g.json"
            h1 = g.save(p)
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", raw)
            g2 = Graph.load(p)
            h2 = g2.save(Path(t) / "g2.json")
            self.assertEqual(h1, h2)
            p.write_bytes(b"\xef\xbb\xbf" + raw)
            with self.assertRaises(ValueError):
                Graph.load(p)


if __name__ == "__main__":
    unittest.main()
