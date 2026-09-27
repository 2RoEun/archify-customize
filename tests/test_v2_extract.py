"""V2 Extractor units on a fixture with hand-derived answers (DESIGN.md §5, §7)."""
import unittest
from pathlib import Path

from minimap.extract import extract

FIX = Path(__file__).resolve().parent / "fixtures"
C, U, D = "fixpkg/core.py", "fixpkg/util.py", "fixpkg/sub/deep.py"
E = "fn:fixpkg.core.Engine"


def S(a, b, kind):
    return (a, b, kind, "static")


EXPECTED_CONTAINS = {
    ("mod:fixpkg", "mod:fixpkg.core"), ("mod:fixpkg", "mod:fixpkg.util"), ("mod:fixpkg", "mod:fixpkg.sub"),
    ("mod:fixpkg.sub", "mod:fixpkg.sub.deep"),
    ("mod:fixpkg.util", "fn:fixpkg.util.helper"), ("mod:fixpkg.util", "fn:fixpkg.util.save_cache"),
    ("mod:fixpkg.util", "cls:fixpkg.util.Worker"), ("cls:fixpkg.util.Worker", "fn:fixpkg.util.Worker.step"),
    ("mod:fixpkg.sub.deep", "fn:fixpkg.sub.deep.deep_fn"),
    ("mod:fixpkg.core", "fn:fixpkg.core.top"), ("mod:fixpkg.core", "fn:fixpkg.core.plain"),
    ("mod:fixpkg.core", "cls:fixpkg.core.Base"), ("mod:fixpkg.core", "cls:fixpkg.core.Engine"),
    ("mod:fixpkg.core", "fn:fixpkg.core.writer"), ("cls:fixpkg.core.Base", "fn:fixpkg.core.Base.ping"),
    *{("cls:fixpkg.core.Engine", f"{E}.{m}") for m in ("__init__", "run", "step", "load", "save", "deco", "aio")},
    (f"{E}.step", f"{E}.step.inner"),
}
EXPECTED_IMPORTS = {
    ("mod:fixpkg", "cls:fixpkg.core.Engine"): ["fixpkg/__init__.py:2"],
    ("mod:fixpkg.core", "mod:fixpkg.util"): [f"{C}:4", f"{C}:5"],
    ("mod:fixpkg.core", "fn:fixpkg.util.helper"): [f"{C}:5"],
    ("mod:fixpkg.core", "fn:fixpkg.sub.deep.deep_fn"): [f"{C}:6"],
    ("mod:fixpkg.sub.deep", "fn:fixpkg.util.helper"): [f"{D}:1"],
}
EXPECTED_CALLS = {  # (from, to): (grade, evidence)
    ("fn:fixpkg.util.Worker.step", "fn:fixpkg.util.helper"): ("static", [f"{U}:15"]),
    ("fn:fixpkg.sub.deep.deep_fn", "fn:fixpkg.util.helper"): ("static", [f"{D}:5"]),
    ("fn:fixpkg.core.top", "fn:fixpkg.util.helper"): ("static", [f"{C}:10"]),          # rule 2 (alias hp)
    ("fn:fixpkg.core.plain", "fn:fixpkg.core.top"): ("static", [f"{C}:14"]),           # rule 1
    ("fn:fixpkg.core.plain", "fn:fixpkg.util.helper"): ("static", [f"{C}:15"]),        # rule 4
    ("fn:fixpkg.core.plain", "fn:fixpkg.sub.deep.deep_fn"): ("static", [f"{C}:16"]),   # rule 2
    (f"{E}.run", f"{E}.step"): ("static", [f"{C}:31", f"{C}:33", f"{C}:35"]),          # rule 3, 5, 6
    (f"{E}.run", "fn:fixpkg.core.Base.ping"): ("static", [f"{C}:32"]),                 # rule 3 via base
    (f"{E}.run", "cls:fixpkg.core.Engine"): ("static", [f"{C}:34"]),                   # rule 5 constructor
    (f"{E}.run", "fn:fixpkg.core.top"): ("static", [f"{C}:37"]),                       # comprehension
    # (run -> Worker.step, guess) was removed by Phase 4 STEP 1: other = Engine("x") makes other.step() certain
    (f"{E}.step", f"{E}.step.inner"): ("static", [f"{C}:42"]),                         # nested local def
    (f"{E}.step.inner", "fn:fixpkg.core.plain"): ("static", [f"{C}:41"]),
    (f"{E}.aio", f"{E}.run"): ("static", [f"{C}:55"]),
}
EXPECTED_PATHS = {  # (from, to, kind): evidence
    ("mod:fixpkg.util", "data:state/cache.json", "uses_path"): [f"{U}:2"],
    ("fn:fixpkg.util.save_cache", "data:state/cache.json", "writes"): [f"{U}:10"],
    ("mod:fixpkg.util", "data:.state.lock", "uses_path"): [f"{U}:18"],   # hidden file keeps its dot
    (f"{E}.__init__", "data:data/events.jsonl", "uses_path"): [f"{C}:28"],
    (f"{E}.load", "data:data/events.jsonl", "reads"): [f"{C}:45"],
    (f"{E}.save", "data:data/events.jsonl", "writes"): [f"{C}:48"],
    ("fn:fixpkg.core.writer", "data:out.json", "writes"): [f"{C}:59"],
    ("fn:fixpkg.core.writer", "data:in.txt", "reads"): [f"{C}:61"],
}


class V2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = extract(FIX, "fixpkg", extra_dirs={})
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.g.edges.values()}

    def by_kind(self, kind):
        return {k: e for k, e in self.edges.items() if k[2] == kind}

    def test_v2_1_nodes_and_lines(self):
        n = self.g.nodes
        kinds = {}
        for node in n.values():
            kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
        self.assertEqual(kinds, {"module": 5, "function": 16, "class": 3, "data": 5})
        self.assertEqual((n[f"{E}.deco"]["line"], n[f"{E}.deco"]["first"]), (51, 50))   # decorated
        self.assertTrue(n[f"{E}.aio"]["is_async"])
        self.assertEqual((n[f"{E}.step.inner"]["line"], n[f"{E}.step.inner"]["end"]), (40, 41))
        self.assertEqual((n["cls:fixpkg.core.Engine"]["line"], n["cls:fixpkg.core.Engine"]["end"]), (26, 55))
        self.assertTrue(n[f"{E}.run"]["method"])
        self.assertEqual(n["mod:fixpkg.sub.deep"]["file"], D)

    def test_v2_2_contains(self):
        got = {(a, b) for (a, b, k), e in self.by_kind("contains").items()}
        self.assertEqual(got, EXPECTED_CONTAINS)
        self.assertTrue(all(e["grade"] == "static" for e in self.by_kind("contains").values()))

    def test_v2_3_imports(self):
        got = {(a, b): e["evidence"] for (a, b, k), e in self.by_kind("imports").items()}
        self.assertEqual(got, EXPECTED_IMPORTS)

    def test_v2_4_calls(self):
        got = {(a, b): (e["grade"], e["evidence"]) for (a, b, k), e in self.by_kind("calls").items()}
        self.assertEqual(got, EXPECTED_CALLS)
        self.assertNotIn((f"{E}.run", "fn:fixpkg.util.Worker.step", "calls"), self.edges)  # typed: no false guess
        self.assertEqual(self.g.stats["unresolved_calls"], 1)   # self.missing()
        self.assertEqual(self.g.stats["skipped_common"], 5)     # write, read_text, write_text, write, read
        self.assertEqual(self.g.stats["parse_errors"], [])

    def test_v2_5_paths(self):
        got = {k: e["evidence"] for k, e in self.edges.items() if k[2] in ("reads", "writes", "uses_path")}
        self.assertEqual(got, EXPECTED_PATHS)
        self.assertTrue(all(self.edges[k]["grade"] == "guess" for k in EXPECTED_PATHS))

    def test_v2_total(self):
        self.assertEqual(len(self.g.edges), 23 + 5 + 13 + 8)
        self.assertEqual(set(self.g.sources), {"fixpkg/__init__.py", C, U, D, "fixpkg/sub/__init__.py"})


if __name__ == "__main__":
    unittest.main()
