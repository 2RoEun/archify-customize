"""Planted-mutant check for the Phase 4 STEP tests: each test module must fail on deliberately wrong code.

python mutants.py            (all steps)
python mutants.py s9 s8      (some steps)
python mutants.py --selftest (prove the memory cap works, then exit)

Each mutant copies minimap/ and tests/ to a temp folder, applies one exact text replacement (it must match exactly
once), and runs the STEP's test module there. CAUGHT = the tests did not pass. Exit 1 if any mutant is MISSED or
cannot be applied.

Safety (2026-09-26 incident: a STEP 9 mutant without a visited set grew one python.exe to about 150 GB of virtual
memory and froze the PC; stopping the shell did not stop python): every run has a time limit, and on Windows this
process and all its children share one job object with a memory cap and kill-on-close, so a runaway mutant hits
MemoryError instead of the whole machine, and nothing outlives this process.
Modified: 2026-09-26 / author: Claude Opus 5.5 (effort level not reported by the tool).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 90
MEMORY_MB = 1536

MUTANTS = {
    "s6": ("minimap/query.py", "tests.test_s6_coverage", [
        ("traced always true", 'return any(n.get("ran") for n in self.nodes.values())', "return True"),
        ("never-ran list keeps run functions", "never = sorted(set(fns) - set(ran), key=self.sort_key)",
         "never = sorted(set(fns), key=self.sort_key)"),
        ("box functions counted as outside", "            inside.update(fns)", "            pass"),
    ]),
    "s7": ("minimap/query.py", "tests.test_s7_arrows", [
        ("dashed not declared", 'if variant == "dashed":', "if False:"),
        ("internal edges counted", ' and e["to"] not in inside_a]', "]"),
        ("worst grade first", 'TRUST_ORDER = ("verified", "static", "runtime-only", "guess")',
         'TRUST_ORDER = ("guess", "runtime-only", "static", "verified")'),
        ("direction swapped", "fwd, rev = self.edges_between(a, b), self.edges_between(b, a)",
         "rev, fwd = self.edges_between(a, b), self.edges_between(b, a)"),
    ]),
    "s8": ("minimap/query.py", "tests.test_s8_hotspots", [
        ("untested = share", 'untested = round(1 - cov["share"], 4)', 'untested = round(cov["share"], 4)'),
        ("reach = level count", 'reach = sum(len(lvl) for lvl in self.impact(nid, min_grade)["levels"])',
         'reach = len(self.impact(nid, min_grade)["levels"])'),
        ("lowest first", 'rows.sort(key=lambda r: (-r["score"], self.sort_key(r["id"])))',
         'rows.sort(key=lambda r: (r["score"], self.sort_key(r["id"])))'),
        ("reach + untested", 'rows.append({"id": nid, "score": round(reach * untested, 4)',
         'rows.append({"id": nid, "score": round(reach + untested, 4)'),
        ("CLI hotspot floor falls back to guess", 'a.min_grade = a.min_grade or "static"',
         'a.min_grade = a.min_grade or "guess"'),
    ]),
    "s10": ("minimap/query.py", "tests.test_s10_dataflow", [
        ("no seen set on file chains", 'if e["kind"] == ("writes" if down else "reads") and e["to"] not in seen:',
         'if e["kind"] == ("writes" if down else "reads"):'),
        ("no depth bound", "depth = max(0, min(depth, self.FLOW_MAX_DEPTH))", "depth = max(0, depth)"),
        ("chain direction swapped", 'for fn in (r["id"] for r in self._flow_rows(f, "reads" if down else "writes")):',
         'for fn in (r["id"] for r in self._flow_rows(f, "writes" if down else "reads")):'),
        ("path users counted as readers", '"readers": self._flow_rows(nid, "reads")',
         '"readers": self._flow_rows(nid, "uses_path")'),
    ]),
    "s10x": ("minimap/extract.py", "tests.test_s10_dataflow", [
        ("joined paths not used", "        joined[id(tail[-1])] = text", "        pass"),
        ("names bound twice still tracked", "if count.get(k) == 1 and k not in params}",
         "if count.get(k, 0) >= 1 and k not in params}"),
        ("local path names off", "        scope.local_paths = {k: v for k, v in value.items()",
         "        scope.local_paths = {k: v for k, v in {}.items()"),
    ]),
    "s11": ("minimap/query.py", "tests.test_s11_diff", [
        ("added and removed swapped", '"nodes_added": sorted(set(nn) - set(on))', '"nodes_added": sorted(set(on) - set(nn))'),
        ("grade changes ignored", 'for k in sorted(set(oe) & set(ne)) if oe[k]["grade"] != ne[k]["grade"]]',
         "for k in sorted(set(oe) & set(ne)) if False]"),
        ("ran changes ignored", 'if bool(on[n].get("ran")) != bool(nn[n].get("ran"))]', "if False]"),
        ("removed edges dropped", '"edges_removed": [row(oe[k]) for k in sorted(set(oe) - set(ne))]',
         '"edges_removed": []'),
    ]),
    "s14": ("minimap/gate.py", "tests.test_s14_gate", [
        ("order ignored (every pair counted as parallel)", "            if a in anc[b] or b in anc[a]:\n                continue",
         "            if False:\n                continue"),
        ("numbers outside the INDEX counted", 'if m and (len(parts) == 1 or parts[0] in ("docs", "assets")):', "if m:"),
        ("paths compared case-sensitively", 'split("/") if p not in ("", ".")).lower()', 'split("/") if p not in ("", "."))'),
        ("repeat_reason ignored", ' and not str(u.get("repeat_reason") or "").strip():', ":"),
        ("cycle check off", "if not any(x in left for x in preds)", "if True"),
        ("duplicate id hides the first unit", 'and u["id"] in ids and u["id"] not in known:', 'and u["id"] in ids:'),
    ]),
    "notes": ("minimap/query.py", "tests.test_agent_notes", [
        ("impact lists read files instead of written ones", 'if e["kind"] == "writes" and e["to"].startswith("data:")})',
         'if e["kind"] == "reads" and e["to"].startswith("data:")})'),
        ("child-process note only for boxes", 'if (r["never_ran"] if a.node else r["total"]["never_ran"]):',
         "if never:"),
        ("path warning never shown", 'if any(s["grade"] == "runtime-only" for s in r["steps"]):', "if False:"),
        ("readers of written files not listed", 'r["file_readers"] = ix.file_readers(r["writes_files"])',
         'r["file_readers"] = {}'),
        ("list-form -m not recognised", """CHILD_RE = (re.compile(r\"\"\"["']-m["']\\s*,""",
         """CHILD_RE = (re.compile(r\"\"\"["']-X["']\\s*,"""),
        ("reader callers not listed", 'x["callers"] = sorted({e["from"] for e in self.inc.get(x["id"], []) if e["kind"] == "calls"},',
         'x["callers"] = sorted(set(),'),
        ("entry points not found", 'if e["kind"] == "calls" and e["to"].startswith("fn:"):', "if False:"),
    ]),
    "s9": ("minimap/query.py", "tests.test_s9_path", [
        ("no visited set (runaway on cycles)", '                    if e["to"] not in prev:',
         "                    if True:"),
        ("worst edge per hop", 'k = (self.TRUST_ORDER.index(e["grade"]), e["kind"])',
         'k = (-self.TRUST_ORDER.index(e["grade"]), e["kind"])'),
        ("grade floor ignored", 'if e["kind"] in DEP_KINDS and self.GRADE_RANK.get(e["grade"], 0) >= floor:',
         'if e["kind"] in DEP_KINDS:'),
        ("steps not reversed", "        steps.reverse()", "        pass"),
        ("start without inside nodes", "level = sorted(self.descendants(a), key=self.sort_key)", "level = [a]"),
    ]),
}


def cap_this_process_tree(memory_mb: int) -> bool:
    """Windows: job object on this process (children inherit it) with a total memory cap and kill-on-close."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
    info.JobMemoryLimit = memory_mb * 1024 * 1024
    ok = bool(job) and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
        and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())  # 9 = JobObjectExtendedLimitInformation
    if ok:
        globals()["_JOB"] = job  # kept open for this process's life; the OS closes it on exit
    return bool(ok)


def run_one(target: str, module: str, old: str | None, new: str | None) -> str:
    """old=None runs the untouched copy (the harness check: it must PASS, or every CAUGHT would mean nothing)."""
    with tempfile.TemporaryDirectory() as t:
        for d in ("minimap", "tests"):
            shutil.copytree(HERE / d, Path(t) / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in HERE.glob("*.py"):  # run.py, mm.py, ... (end-to-end tests call them from the copy)
            shutil.copy2(f, Path(t) / f.name)
        if old is not None:
            p = Path(t) / target
            src = p.read_text(encoding="utf-8")
            if src.count(old) != 1:
                return f"NOT APPLIED (pattern found {src.count(old)} times)"
            p.write_text(src.replace(old, new), encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, "-B", "-m", "unittest", module], cwd=t, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return f"CAUGHT (no result within {TIMEOUT_S} s)"
        if "MemoryError" in r.stderr:
            return f"CAUGHT (hit the {MEMORY_MB} MB cap)"
        return "MISSED" if r.returncode == 0 else f"CAUGHT ({(r.stderr.strip().splitlines() or ['?'])[-1]})"


def selftest() -> int:
    """A child that tries to take 2 GB (more than the cap) must get MemoryError."""
    r = subprocess.run([sys.executable, "-c", "b = bytearray(2 * 1024 ** 3)"], capture_output=True, text=True,
                       timeout=60)
    ok = r.returncode != 0 and "MemoryError" in r.stderr
    print(f"memory cap self-test: {'PASS' if ok else 'FAIL'} (child exit {r.returncode})")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    capped = cap_this_process_tree(MEMORY_MB)
    print(f"safety: time limit {TIMEOUT_S} s per run; memory cap {MEMORY_MB} MB for all children: "
          f"{'on' if capped else 'OFF (not Windows or job setup failed)'}")
    if "--selftest" in argv:
        return selftest()
    if not capped:
        print("refusing to run mutants without the memory cap")
        return 1
    bad = 0
    for step in [a for a in argv if not a.startswith("-")] or list(MUTANTS):
        target, module, muts = MUTANTS[step]
        if run_one(target, module, None, None) != "MISSED":  # "MISSED" = the untouched copy passes
            bad += 1
            print(f"{step} HARNESS BROKEN: the untouched copy does not pass {module}; its mutants are not run")
            continue
        for label, old, new in muts:
            verdict = run_one(target, module, old, new)
            bad += not verdict.startswith("CAUGHT")
            print(f"{step} {label}: {verdict}", flush=True)
    print("all mutants caught" if not bad else f"{bad} mutant(s) not caught or not applied")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
