"""零一拾文的原子文件写入与稳定资源命名。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def append_jsonl(path: Path, record: dict) -> None:
    """先持久化索引，再允许游标确认；隔开旧的中断残行。"""
    with Path(path).open("a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
        fh.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def atomic_write(path: Path, content: str | bytes, *, private: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8") if isinstance(content, str) else content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp, 0o600 if private else 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
