import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vbank.accounts import Accounts
from vbank.reports import summary

ROOT = Path(__file__).resolve().parents[1]


class VBank(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "data").mkdir()
            a = Accounts(t)
            a.deposit("kim", 10)
            a.withdraw("kim", 3)
            self.assertEqual(summary(t, ["kim"]), {"kim": 7})

    def test_cli(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "data").mkdir()
            Accounts(t).deposit("lee", 5)
            out = subprocess.run([sys.executable, "-m", "vbank.cli", t, "lee"], capture_output=True, text=True,
                                 timeout=60, cwd=str(ROOT))
            self.assertIn("'lee': 5", out.stdout)
