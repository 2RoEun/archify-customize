"""STEP 4 (Phase 4) — panel.js impact = query.py impact for every node x grade floor x level cap on two graphs."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.query import Index
from tests.test_v4_trees import NODE

HERE = Path(__file__).resolve().parent
GRADES = ("verified", "static", "guess")
CAPS = (10, 1)


def graphs():
    g = extract(HERE / "fixtures", "fixpkg", extra_dirs={})
    g.add_node("concept:util", "concept", "util")
    g.add_node("concept:core", "concept", "core")
    g.add_edge("concept:util", "mod:fixpkg.util", "maps", static="confirmed")
    g.add_edge("concept:core", "mod:fixpkg.core", "maps", static="confirmed")
    # runtime-merged fixture with tests, so all four grades and the test list take part
    rt = extract(HERE / "fixtures" / "rt", "rtpkg", extra_dirs={"tests": "test"})
    rt.add_edge("fn:rtpkg.mod.use_box", "fn:rtpkg.mod.Box.__enter__", "calls", runtime=True, evidence="x:1")
    rt.add_edge("fn:rtpkg.mod.use_box", "fn:rtpkg.mod.Box.get_value", "calls", runtime=True, evidence="x:2")
    rt.add_node("concept:mod", "concept", "mod")
    rt.add_edge("concept:mod", "mod:rtpkg.mod", "maps", static="confirmed")
    assert "mod:tests.test_rt" in rt.nodes
    rt.add_node("concept:tests", "concept", "tests")
    rt.add_edge("concept:tests", "mod:tests.test_rt", "maps", static="confirmed")
    return [g.to_dict(), rt.to_dict()]


def run_js(data: dict, tmp: Path, name: str, panel: Path | None = None) -> dict:
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    cmd = [NODE, str(HERE / "run_impact.js"), str(p)] + ([str(panel)] if panel else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def run_py(data: dict) -> dict:
    ix = Index(data)
    return {f"{n['id']}|{g}|{m}": ix.impact(n["id"], g, m) for n in data["nodes"] for g in GRADES for m in CAPS}


class Step4Parity(unittest.TestCase):
    def test_s4_1_js_equals_py(self):
        if not Path(NODE).is_file():
            self.fail(f"node.exe not found at {NODE}")
        with tempfile.TemporaryDirectory() as t:
            for k, data in enumerate(graphs()):
                js, py = run_js(data, Path(t), f"g{k}"), run_py(data)
                self.assertEqual(set(js), set(py))
                diff = [key for key in py if py[key] != js[key]]
                self.assertEqual(diff, [], f"graph {k}: {len(diff)} results differ, first {diff[:3]}")
                # the comparison must cover real work, not only empty answers
                self.assertGreater(len(py), 100)
                deepest = max(len(r["levels"]) for r in py.values())
                self.assertGreaterEqual(deepest, (3, 2)[k], f"graph {k}: spread too shallow")
                self.assertTrue(any(len(r["boxes"]) >= 2 for r in py.values()), f"graph {k}: no multi-box result")
                # each grade floor must change answers somewhere: static < guess on the fixture (guess data edges),
                # verified < static on rt (its only guess edge was upgraded by the added runtime evidence)
                reach = {g: sum(len(x) for key, r in py.items() if key.endswith(f"|{g}|10") for x in r["levels"])
                         for g in GRADES}
                if k == 0:
                    self.assertLess(reach["static"], reach["guess"])
                else:
                    self.assertTrue(any(r["tests"] for r in py.values()), "rt graph: no test reached")
                    self.assertLess(reach["verified"], reach["static"])

    def test_s4_2_same_fixture_answers_as_step3(self):
        if not Path(NODE).is_file():
            self.fail(f"node.exe not found at {NODE}")
        with tempfile.TemporaryDirectory() as t:
            js = run_js(graphs()[0], Path(t), "g0")
        self.assertEqual([len(x) for x in js["fn:fixpkg.util.helper|guess|10"]["levels"]], [6, 2, 2])
        self.assertEqual(js["concept:util|guess|10"]["boxes"], {"concept:core": 1, "concept:util": 0})
        self.assertEqual(js["data:data/events.jsonl|static|10"]["levels"], [])


if __name__ == "__main__":
    unittest.main()
