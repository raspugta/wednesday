"""Filesystem tools. Path checks happen in Permissions; these functions do the I/O."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

MAX_READ_BYTES = 200_000
TEXT_SNIFF = 4096


def _looks_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        chunk = f.read(TEXT_SNIFF)
    return b"\0" in chunk


def read_file(path: Path, start_line: int = 1, max_lines: int = 200) -> str:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory — use list_dir")
    if path.stat().st_size > 50_000_000:
        raise ValueError(f"{path} is too large ({path.stat().st_size // 1_000_000} MB)")
    if _looks_binary(path):
        return f"[{path} is a binary file, {path.stat().st_size} bytes]"
    with open(path, "r", errors="replace") as f:
        lines = f.read(MAX_READ_BYTES).splitlines()
    start = max(1, int(start_line))
    chunk = lines[start - 1 : start - 1 + max(1, min(int(max_lines), 400))]
    body = "\n".join(f"{i:5d}  {line}" for i, line in enumerate(chunk, start))
    total = len(lines)
    more = f"\n[... {total - (start - 1 + len(chunk))} more lines; call again with start_line]" \
        if start - 1 + len(chunk) < total else ""
    return f"{path} ({total} lines)\n{body}{more}"


def list_dir(path: Path, show_hidden: bool = False, limit: int = 200) -> str:
    if not path.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")
    entries = []
    for e in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not show_hidden and e.name.startswith("."):
            continue
        try:
            kind = "dir " if e.is_dir() else f"{e.stat().st_size:>9}"
        except OSError:
            kind = "  ?"
        entries.append(f"{kind}  {e.name}{'/' if e.is_dir() else ''}")
        if len(entries) >= limit:
            entries.append(f"[... truncated at {limit} entries]")
            break
    return f"{path}:\n" + ("\n".join(entries) or "(empty)")


def search_files(root: Path, name_glob: str = "*", contains: str = "", max_results: int = 40,
                 is_allowed=lambda p: True) -> str:
    """Find files under root by name glob, optionally containing a text snippet."""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "install", "log", ".cache"}
    results = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            scanned += 1
            if scanned > 50_000:
                results.append("[stopped after scanning 50k files]")
                return "\n".join(results)
            if not fnmatch.fnmatch(fn, name_glob):
                continue
            p = Path(dirpath) / fn
            if not is_allowed(p):
                continue
            if contains:
                try:
                    if p.stat().st_size > 2_000_000 or _looks_binary(p):
                        continue
                    with open(p, "r", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if contains.lower() in line.lower():
                                results.append(f"{p}:{i}: {line.strip()[:160]}")
                                break
                        else:
                            continue
                except OSError:
                    continue
            else:
                results.append(str(p))
            if len(results) >= max_results:
                results.append(f"[limited to {max_results} results]")
                return "\n".join(results)
    return "\n".join(results) or "no matches"


def write_file(path: Path, content: str, append: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(path, mode) as f:
        f.write(content)
    return f"{'appended' if append else 'wrote'} {len(content)} chars to {path}"
