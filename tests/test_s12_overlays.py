"""STEP 12 (Phase 4) — overlay data per box (Python side; the browser check compares panel.js against this)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.query import Index
from tests.test_s11_diff import mutate

HERE = Path(__file__).resolve().parent
CONN = [{"id": "core-to-util", "from": "core", "to": "util"},
        {"id": "util-to-core", "from": "util", "to": "core", "variant": "dashed"}]


def boxed(root: Path) -> dict:
    g = extract(root, "fixpkg", extra_dirs={})
    for c, m in (("core", "mod:fixpkg.core"), ("util", "mod:fixpkg.util")):
        g.add_node(f"concept:{c}", "concept", c)
        g.add_edge(f"concept:{c}", m, "maps", static="confirmed")
    return g.to_dict()


class Step12Overlays(unittest.TestCase):
    def test_s12_1_overlay_data(self):
        with tempfile.TemporaryDirectory() as t:
            shutil.copytree(HERE / "fixtures" / "fixpkg", Path(t) / "fixpkg",
                            ignore=shutil.ignore_patterns("__pycache__"))
            g0 = boxed(Path(t))
            mutate(Path(t) / "fixpkg" / "util.py", 'LOCK_FILE = ".state.lock"\n', 'LOCK_FILE = ".state.lock"\n',
                   "\n\ndef added():\n    return helper(5)\n")
            mutate(Path(t) / "fixpkg" / "core.py", "    deep_fn()\n", "    pass\n")
            g1 = boxed(Path(t))
        # g1 marks: plain and helper ran, nothing else; so hotspots are the never-run functions that others use
        for n in g1["nodes"]:
            if n["id"] in ("fn:fixpkg.core.plain", "fn:fixpkg.util.helper"):
                n["ran"] = True
        ov = Index(g1).overlays(CONN, g0)
        # util: new node added() + its call to helper, both inside util; core: plain -> deep_fn removed (sub has no box)
        # ran flags of plain and helper changed from False to True
        self.assertEqual(ov["diff"]["boxes"], {"concept:core": {"added": 0, "removed": 1, "changed": 1},
                                               "concept:util": {"added": 2, "removed": 0, "changed": 1}})
        self.assertEqual(ov["arrows"], {"core-to-util": {"trust": "static", "edges": 4},
                                        "util-to-core": {"trust": "declared", "edges": 0}})
        # util functions: helper, save_cache, Worker.step, added -> 1 of 4 ran; core: 11 functions, plain ran
        self.assertEqual(ov["coverage"]["concept:util"], {"functions": 4, "ran": 1, "share": 0.25})
        self.assertEqual(ov["coverage"]["concept:core"]["ran"], 1)
        # hotspots per box = functions with score > 0 whose box it is (same numbers as Index.hotspots)
        rows = {r["id"]: r["score"] for r in Index(g1).hotspots("function", "static", 0)}
        for b, h in ov["hotspots"].items():
            mine = {i: s for i, s in rows.items() if b in Index(g1).boxes_of(i)}
            self.assertEqual((h["count"], h["top"]), (len(mine), max(mine.values())))
        # util's never-run functions (save_cache, Worker.step, added) are used by nobody: reach 0, so no hotspot
        self.assertEqual(sorted(ov["hotspots"]), ["concept:core"])

    def test_s12_2_no_previous_graph(self):
        ov = Index(boxed(HERE / "fixtures")).overlays(CONN, None)
        self.assertIsNone(ov["diff"])
        self.assertEqual(ov["hotspots"], {})  # untraced: no shares, so no hotspots
        self.assertIsNone(ov["coverage"]["concept:util"]["share"])


if __name__ == "__main__":
    unittest.main()
