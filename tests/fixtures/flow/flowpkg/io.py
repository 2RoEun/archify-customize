"""STEP 10 fixture: file paths joined with / or os.path.join, and paths held in local names."""
import os


def write_log(root):
    with (root / "data" / "log.jsonl").open("a") as f:
        f.write("x")


def read_log(root):
    path = root / "data" / "log.jsonl"
    return path.read_text()


def copy_log(root):
    src = os.path.join(root, "data", "log.jsonl")
    dst = root / "out" / "copy.json"
    data = open(src).read()
    open(dst, "w").write(data)


def rebinds(root):
    p = root / "data" / "log.jsonl"
    p = root / "other.txt"
    return p.read_text()


def other_folder(run):
    return (run / "log.jsonl").read_text()
