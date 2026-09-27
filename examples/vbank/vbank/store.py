"""Append-only store for account events."""
import json
from pathlib import Path


class Store:
    def __init__(self, root):
        self.path = Path(root) / "data" / "events.jsonl"

    def append(self, event):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def read_all(self):
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]

    def locked(self, operation):
        """Shared helper: runs whatever callback the caller passes."""
        return operation()
