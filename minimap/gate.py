"""Parallel gate: checks to run before several work units (people or AI agents) run at the same time.

python gate.py PLAN.json [--root PROJECT] [--json] [--out RESULT.json]

PLAN.json = {"units": [{"id": "U1", "after": [], "writes": ["docs/x.md"], "locks": ["db"], "outputs": ["OUT-1"],
             "checks": ["unit suite"], "repeat_reason": "", "facts": [{"fact": "release date", "place": "STATUS.md"}],
             "external": false, "waits": ["review reply"]}]}
Only "id" and "after" are required ("after": [] when a unit has no predecessor).

Linkage  L1 every unit lists its predecessors and they exist; L2 no cycle; L3 no two units that can run at the same
         time (neither comes after the other) write the same file, take the same lock, or add numbered files with
         the same NNN (for projects that keep one global number per NNN_name file across the root, docs/ and
         assets/); with --root, a new numbered file must not reuse a number already on disk; L4 every external
         wait is named.
Duplication D1 unit ids unique; D2 output ids unique; D3 a repeated check states why; D4 one fact, one place.
Exit 0 PASS (the result lists waves that may run in parallel), 1 FAIL (run in sequence or fix the plan), 2 bad input.
Safety: at most MAX_UNITS units; every walk over "after" keeps a visited set, so cycles cannot loop.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_UNITS = 1000
NUM_RE = re.compile(r"^(\d{3})_")
CHECKS = ("L1_predecessors", "L2_no_cycle", "L3_no_parallel_write_conflict", "L4_external_waits_named",
          "D1_unique_unit_ids", "D2_unique_outputs", "D3_repeated_checks_explained", "D4_one_place_per_fact")


def norm(path: str) -> str:
    """Windows and POSIX spellings of one repo path compare equal (case-insensitive, like NTFS)."""
    return "/".join(p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")).lower()


def indexed_number(path: str) -> str | None:
    """NNN of a numbered file at the root or anywhere under docs/ or assets/ (one shared number space)."""
    parts = norm(path).split("/")
    m = NUM_RE.match(parts[-1])
    if m and (len(parts) == 1 or parts[0] in ("docs", "assets")):
        return m.group(1)
    return None


def numbers_on_disk(root: Path) -> dict:
    found: dict = {}
    files = [p for p in root.glob("[0-9][0-9][0-9]_*") if p.is_file()]
    for d in ("docs", "assets"):
        if (root / d).is_dir():
            files += [p for p in (root / d).rglob("[0-9][0-9][0-9]_*") if p.is_file()]
    for p in files:
        found.setdefault(p.name[:3], norm(p.relative_to(root).as_posix()))
    return found


def _ancestors(uid: str, after: dict) -> set:
    seen, stack = set(), list(after.get(uid, ()))
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in after:
            continue
        seen.add(cur)
        stack.extend(after[cur])
    return seen


def gate(plan: dict, root: Path | None = None) -> dict:
    units = plan.get("units") if isinstance(plan, dict) else None
    if not isinstance(units, list) or not units or not all(isinstance(u, dict) for u in units):
        raise ValueError("the plan needs a non-empty 'units' list of objects")
    if len(units) > MAX_UNITS:
        raise ValueError(f"{len(units)} units; the gate takes at most {MAX_UNITS}")
    p = {k: [] for k in CHECKS}
    ids = []
    for n, u in enumerate(units):
        uid = u.get("id")
        if not isinstance(uid, str) or not uid.strip():
            p["D1_unique_unit_ids"].append(f"unit #{n + 1} has no id")
        elif uid in ids:
            p["D1_unique_unit_ids"].append(f"{uid} is used by more than one unit")
        else:
            ids.append(uid)
    known: dict = {}  # the first unit with each id (a repeated id is a D1 problem; it must not hide the first unit)
    for u in units:
        if isinstance(u.get("id"), str) and u["id"] in ids and u["id"] not in known:
            known[u["id"]] = u
    after = {}
    for uid, u in known.items():
        if not isinstance(u.get("after"), list):
            p["L1_predecessors"].append(f"{uid}: no 'after' list (write [] when it has no predecessor)")
        preds = [x for x in u.get("after") or [] if isinstance(x, str)]
        for x in preds:
            if x == uid:
                p["L1_predecessors"].append(f"{uid} lists itself as a predecessor")
            elif x not in known:
                p["L1_predecessors"].append(f"{uid}: predecessor {x} is not a unit in this plan")
        after[uid] = [x for x in preds if x in known and x != uid]
    # L2: waves by Kahn's algorithm; units left over sit on a cycle
    left, waves = dict(after), []
    while left:
        ready = sorted(uid for uid, preds in left.items() if not any(x in left for x in preds))
        if not ready:
            p["L2_no_cycle"].append("cycle among: " + ", ".join(sorted(left)))
            break
        waves.append(ready)
        for uid in ready:
            del left[uid]
    # L3: pairs that may run at the same time
    anc = {uid: _ancestors(uid, after) for uid in known}
    files = {uid: {norm(f) for f in known[uid].get("writes") or []} for uid in known}
    locks = {uid: {str(x).strip().lower() for x in known[uid].get("locks") or []} for uid in known}
    nums = {uid: {} for uid in known}
    for uid in known:
        for f in known[uid].get("writes") or []:
            num = indexed_number(f)
            if num:
                nums[uid].setdefault(num, set()).add(norm(f))
    order = list(known)
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            if a in anc[b] or b in anc[a]:
                continue  # one runs after the other
            for f in sorted(files[a] & files[b]):
                p["L3_no_parallel_write_conflict"].append(f"{a} and {b} may run together and both write {f}")
            for lk in sorted(locks[a] & locks[b]):
                p["L3_no_parallel_write_conflict"].append(f"{a} and {b} may run together and both take lock {lk}")
            for num in sorted(set(nums[a]) & set(nums[b])):
                if nums[a][num] != nums[b][num]:
                    p["L3_no_parallel_write_conflict"].append(
                        f"{a} and {b} may run together and both add number {num}: "
                        f"{sorted(nums[a][num] | nums[b][num])}")
    if root is not None:
        on_disk = numbers_on_disk(Path(root))
        for uid in known:
            for num, paths in sorted(nums[uid].items()):
                for f in sorted(paths):
                    if num in on_disk and on_disk[num] != f and not (Path(root) / f).exists():
                        p["L3_no_parallel_write_conflict"].append(
                            f"{uid} adds {f} but number {num} is already used by {on_disk[num]}")
    # L4
    for uid, u in known.items():
        waits = u.get("waits") or []
        if any(not isinstance(w, str) or not w.strip() for w in waits):
            p["L4_external_waits_named"].append(f"{uid} has an unnamed wait")
        if u.get("external") and not waits:
            p["L4_external_waits_named"].append(f"{uid} waits on something external but names no wait")
    # D2
    owner: dict = {}
    for uid, u in known.items():
        for o in u.get("outputs") or []:
            if o in owner:
                p["D2_unique_outputs"].append(f"output {o} is produced by {owner[o]} and {uid}")
            else:
                owner[o] = uid
    # D3
    first: dict = {}
    for uid, u in known.items():
        for c in u.get("checks") or []:
            key = str(c).strip().lower()
            if key in first and not str(u.get("repeat_reason") or "").strip():
                p["D3_repeated_checks_explained"].append(
                    f"{uid} repeats check '{c}' (first in {first[key]}) without a repeat_reason")
            first.setdefault(key, uid)
    # D4
    places: dict = {}
    for uid, u in known.items():
        for f in u.get("facts") or []:
            if isinstance(f, dict) and f.get("fact") and f.get("place"):
                places.setdefault(str(f["fact"]).strip().lower(), {}).setdefault(norm(f["place"]), uid)
    for fact, where in sorted(places.items()):
        if len(where) > 1:
            p["D4_one_place_per_fact"].append(f"fact '{fact}' is written to {len(where)} places: "
                                              + ", ".join(f"{k} ({v})" for k, v in sorted(where.items())))
    ok = not any(p.values())
    return {"verdict": "PASS" if ok else "FAIL", "units": len(units), "problems": p,
            "waves": waves if not p["L2_no_cycle"] else None}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--root", help="repository root, to check new numbered files against numbers already used")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="also write the result here (keep it as the gate evidence)")
    a = ap.parse_args(argv)
    try:
        r = gate(json.loads(Path(a.plan).read_text(encoding="utf-8")), Path(a.root) if a.root else None)
    except (OSError, ValueError) as exc:
        print(f"bad plan: {exc}", file=sys.stderr)
        return 2
    r["plan"] = str(a.plan)
    if a.out:
        Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print(f"parallel gate: {r['verdict']} ({r['units']} units)")
        for k in CHECKS:
            print(f"  {'ok  ' if not r['problems'][k] else 'FAIL'} {k}")
            for msg in r["problems"][k]:
                print(f"       - {msg}")
        if r["waves"] is not None:
            print("waves (units in one wave may run together): "
                  + " -> ".join("[" + ", ".join(w) + "]" for w in r["waves"]))
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
