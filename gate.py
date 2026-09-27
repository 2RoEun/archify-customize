"""Parallel gate for work units. See minimap/gate.py for the plan format.

python gate.py PLAN.json --root PROJECT --out RESULT.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from minimap.gate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
