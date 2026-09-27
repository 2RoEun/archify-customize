"""STEP 14 (Phase 4) — parallel gate: correct verdicts on conflict and no-conflict plans (hand-written fixtures)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.gate import MAX_UNITS, gate

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
OK_PLAN = {"units": [
    {"id": "U1", "after": [], "writes": ["docs/AW-017/106_A.md"], "outputs": ["O1"], "checks": ["unit suite"]},
    {"id": "U2", "after": [], "writes": ["docs\\AW-017\\notes.md"], "outputs": ["O2"], "external": True,
     "waits": ["Codex reply"]},
    {"id": "U3", "after": ["U1", "U2"], "writes": ["docs/AW-017/106_A.md"], "checks": ["unit suite"],
     "repeat_reason": "re-run after U1 changed the code", "facts": [{"fact": "controller", "place": "STATUS.md"}]},
]}


def units(*us):
    return {"units": [dict({"after": []}, **u) for u in us]}


def bad(r):
    return {k: v for k, v in r["problems"].items() if v}


class Step14Gate(unittest.TestCase):
    def test_s14_1_no_conflict_plan(self):
        r = gate(OK_PLAN)
        self.assertEqual((r["verdict"], bad(r)), ("PASS", {}))
        self.assertEqual(r["waves"], [["U1", "U2"], ["U3"]])
        # a file may be written twice when one unit comes after the other, even through a chain
        chain = units({"id": "A", "writes": ["f.md"]}, {"id": "B", "after": ["A"]}, {"id": "C", "after": ["B"], "writes": ["F.MD"]})
        self.assertEqual(gate(chain)["verdict"], "PASS")

    def test_s14_2_parallel_write_lock_and_number_conflicts(self):
        r = gate(units({"id": "U1", "writes": ["docs/a.md"]}, {"id": "U2", "writes": ["./DOCS\\a.md"]}))
        self.assertEqual(bad(r), {"L3_no_parallel_write_conflict": ["U1 and U2 may run together and both write docs/a.md"]})
        r = gate(units({"id": "U1", "locks": ["db"]}, {"id": "U2", "locks": ["DB"]}))
        self.assertEqual(bad(r), {"L3_no_parallel_write_conflict": ["U1 and U2 may run together and both take lock db"]})
        r = gate(units({"id": "U1", "writes": ["docs/AW-001/012_a.md"]}, {"id": "U2", "writes": ["assets/briefings/012_b.png"]}))
        self.assertEqual(r["problems"]["L3_no_parallel_write_conflict"],
                         ["U1 and U2 may run together and both add number 012: "
                          "['assets/briefings/012_b.png', 'docs/aw-001/012_a.md']"])
        # data/ is outside the shared number space, so its numbers do not clash
        self.assertEqual(gate(units({"id": "U1", "writes": ["docs/012_a.md"]},
                                    {"id": "U2", "writes": ["data/runs/012_x.json"]}))["verdict"], "PASS")

    def test_s14_3_number_already_on_disk(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "docs" / "foundation").mkdir(parents=True)
            (Path(t) / "docs" / "foundation" / "105_MAP.md").write_text("x", encoding="utf-8")
            (Path(t) / "000_INDEX_WORKLOG.md").write_text("x", encoding="utf-8")
            r = gate(units({"id": "U1", "writes": ["docs/AW-017/105_NEW.md"]}), Path(t))
            self.assertEqual(r["problems"]["L3_no_parallel_write_conflict"],
                             ["U1 adds docs/aw-017/105_new.md but number 105 is already used by docs/foundation/105_map.md"])
            self.assertEqual(gate(units({"id": "U1", "writes": ["docs/foundation/105_MAP.md"]}), Path(t))["verdict"], "PASS")
            self.assertEqual(gate(units({"id": "U1", "writes": ["000_NEW.md"]}), Path(t))["verdict"], "FAIL")

    def test_s14_4_cycle_and_predecessors(self):
        r = gate(units({"id": "U1", "after": ["U2"], "writes": ["x.md"]}, {"id": "U2", "after": ["U1"], "writes": ["x.md"]},
                       {"id": "U3"}))
        self.assertEqual(bad(r), {"L2_no_cycle": ["cycle among: U1, U2"]})
        self.assertIsNone(r["waves"])
        r = gate({"units": [{"id": "U1"}, {"id": "U2", "after": ["U9", "U2"]}]})
        self.assertEqual(bad(r), {"L1_predecessors": ["U1: no 'after' list (write [] when it has no predecessor)",
                                                      "U2: predecessor U9 is not a unit in this plan",
                                                      "U2 lists itself as a predecessor"]})  # in plan order

    def test_s14_5_duplication_checks(self):
        r = gate(units({"id": "U1", "outputs": ["O1"]}, {"id": "U1"}, {"id": ""}, {"id": "U2", "outputs": ["O1"]}))
        self.assertEqual(bad(r), {"D1_unique_unit_ids": ["U1 is used by more than one unit", "unit #3 has no id"],
                                  "D2_unique_outputs": ["output O1 is produced by U1 and U2"]})
        r = gate(units({"id": "U1", "checks": ["unit suite"]}, {"id": "U2", "checks": ["Unit suite"]}))
        self.assertEqual(bad(r), {"D3_repeated_checks_explained":
                                  ["U2 repeats check 'Unit suite' (first in U1) without a repeat_reason"]})
        r = gate(units({"id": "U1", "facts": [{"fact": "controller", "place": "STATUS.md"}]},
                       {"id": "U2", "after": ["U1"], "facts": [{"fact": "Controller", "place": "README.md"},
                                                              {"fact": "x", "place": "a.md"}]},
                       {"id": "U3", "after": ["U2"], "facts": [{"fact": "controller", "place": "status.md"}]}))
        self.assertEqual(bad(r), {"D4_one_place_per_fact": ["fact 'controller' is written to 2 places: readme.md (U2), status.md (U1)"]})

    def test_s14_6_external_waits(self):
        r = gate(units({"id": "U1", "external": True}, {"id": "U2", "waits": [" "]}, {"id": "U3", "external": True, "waits": ["JEV run"]}))
        self.assertEqual(bad(r), {"L4_external_waits_named": ["U1 waits on something external but names no wait",
                                                              "U2 has an unnamed wait"]})

    def test_s14_7_bad_input_is_bounded(self):
        for plan in ({}, {"units": []}, {"units": ["U1"]}, [1, 2]):
            with self.assertRaises(ValueError):
                gate(plan)
        with self.assertRaises(ValueError):
            gate({"units": [{"id": f"U{i}", "after": []} for i in range(MAX_UNITS + 1)]})

    def test_s14_8_cli(self):
        with tempfile.TemporaryDirectory() as t:
            ok, conflict, empty = Path(t) / "ok.json", Path(t) / "conflict.json", Path(t) / "empty.json"
            ok.write_text(json.dumps(OK_PLAN), encoding="utf-8")
            conflict.write_text(json.dumps(units({"id": "U1", "writes": ["a.md"]}, {"id": "U2", "writes": ["a.md"]})),
                                encoding="utf-8")
            empty.write_text("{}", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            run = lambda *a: subprocess.run([sys.executable, "-B", str(TOOL / "gate.py"), *a], capture_output=True,
                                            text=True, encoding="utf-8", env=env, timeout=120)
            r = run(str(ok), "--out", str(Path(t) / "result.json"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("parallel gate: PASS (3 units)", r.stdout)
            self.assertIn("waves (units in one wave may run together): [U1, U2] -> [U3]", r.stdout)
            self.assertEqual(json.loads((Path(t) / "result.json").read_text(encoding="utf-8"))["verdict"], "PASS")
            r = run(str(conflict))
            self.assertEqual(r.returncode, 1)
            self.assertIn("FAIL L3_no_parallel_write_conflict", r.stdout)
            self.assertEqual(run(str(empty)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
