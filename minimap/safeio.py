"""Concurrency-safe file I/O (DESIGN.md §9): atomic replace, retrying reads, and a non-blocking run lock.

Windows notes: os.replace over a file that another process is reading can fail with PermissionError
(sharing violation), and opening a file during a replace can fail the same way, so both sides retry briefly.
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

RETRIES = 100
PAUSE = 0.05


class Busy(RuntimeError):
    """Another process holds the lock."""


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(PAUSE)
    finally:
        if tmp.exists():
            tmp.unlink()


def replace_retry(src: Path, dst: Path) -> None:
    for attempt in range(RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(PAUSE)


def temp_sibling(path: Path, tag: str = "tmp") -> Path:
    path = Path(path)
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.{tag}")


def read_bytes_retry(path: Path) -> bytes:
    """Retry only sharing violations on a regular file; a folder (Errno 13 on Windows) fails at once."""
    for attempt in range(RETRIES):
        try:
            return Path(path).read_bytes()
        except PermissionError:
            if attempt == RETRIES - 1 or not _is_file(path):
                raise
            time.sleep(PAUSE)
    raise AssertionError("unreachable")


def _is_file(path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return True  # cannot tell: treat as a busy file and keep retrying


@contextmanager
def run_lock(lock_path: Path):
    """Hold an OS lock for the whole block; raise Busy at once if another process holds it.
    The OS releases the lock when the holder exits or is killed, so a leftover file never blocks."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        f.close()
        raise Busy(f"another minimap build holds {lock_path}") from exc
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
