"""STEP 1 (Phase 4) — type tracking turns certain guesses into static edges, and only certain ones."""
import unittest
from pathlib import Path

from minimap.extract import extract

FIX = Path(__file__).resolve().parent / "fixtures" / "typed"
M, C, F = "fn:tpkg.mod", "cls:tpkg.mod", "tpkg/mod.py"

EXPECTED = {  # (from, to): (grade, evidence)
    (f"{M}.Service.__init__", f"{C}.Store"): ("static", [f"{F}:22"]),
    (f"{M}.Service.run", f"{M}.Store.save"): ("static", [f"{F}:26"]),      # self.store = Store()
    (f"{M}.Service.run", f"{M}.Store.load"): ("static", [f"{F}:26"]),      # self.backup = backup: Store
    (f"{M}.local_var", f"{C}.Store"): ("static", [f"{F}:30"]),
    (f"{M}.local_var", f"{M}.Store.load"): ("static", [f"{F}:31"]),         # s = Store()
    (f"{M}.annotated", f"{M}.Store.save"): ("static", [f"{F}:35"]),         # s: Store
    (f"{M}.ambiguous", f"{C}.Store"): ("static", [f"{F}:39"]),
    (f"{M}.ambiguous", f"{C}.Other"): ("static", [f"{F}:41"]),
    (f"{M}.ambiguous", f"{M}.Store.save"): ("guess", [f"{F}:42"]),          # two classes: stays a guess
    (f"{M}.ambiguous", f"{M}.Other.save"): ("guess", [f"{F}:42"]),
    (f"{M}.ctx", f"{C}.Store"): ("static", [f"{F}:46"]),
    (f"{M}.ctx", f"{M}.Store.load"): ("static", [f"{F}:47"]),               # with Store() as s, __enter__ returns self
    (f"{M}.unknown", f"{M}.Store.save"): ("guess", [f"{F}:51"]),            # no type information
    (f"{M}.unknown", f"{M}.Other.save"): ("guess", [f"{F}:51"]),
    (f"{M}.reassigned_none", f"{C}.Store"): ("static", [f"{F}:55"]),
    (f"{M}.reassigned_none", f"{M}.Store.load"): ("guess", [f"{F}:57"]),    # later set to None: not certain
}


class Step1Types(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = extract(FIX, "tpkg", extra_dirs={})

    def test_s1_1_calls_exact(self):
        got = {(e["from"], e["to"]): (e["grade"], e["evidence"]) for e in self.g.edges.values() if e["kind"] == "calls"}
        self.assertEqual(got, EXPECTED)

    def test_s1_2_counters(self):
        self.assertEqual((self.g.stats["typed_calls"], self.g.stats["typed_miss"]), (5, 0))

    def test_s1_3_earlier_fixtures_unchanged(self):
        # the V2 fixture has no type-certain calls except none: its exact answers must still hold
        from tests.test_v2_extract import EXPECTED_CALLS
        g = extract(Path(__file__).resolve().parent / "fixtures", "fixpkg", extra_dirs={})
        got = {(e["from"], e["to"]): (e["grade"], e["evidence"]) for e in g.edges.values() if e["kind"] == "calls"}
        self.assertEqual(got, EXPECTED_CALLS)


if __name__ == "__main__":
    unittest.main()
