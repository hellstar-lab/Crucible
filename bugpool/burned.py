"""Unseen discipline: a line that has been SHOWN (to a participant, in a report, in a commit message) is burned and never
pooled again. Permanent: every generate_pool() applies the registry, and every Session.end() burns the line it planted.

A line is burned by (path, line number) and, to survive line shifts, by (path, the line's text). Burning is per LINE,
not per mutation: a different mutation of a line that has been shown is still a line the reader has seen.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DEFAULT = Path(".bugpool") / "burned.json"  # a dot-directory: never listed, read or searched by the session tools
_MENTION = re.compile(r"((?:[\w.-]+/)*[\w.-]+\.py):(\d+)")


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        d = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.lines: set[tuple[str, int]] = {(p, int(n)) for p, n in d.get("lines", [])}
        self.texts: set[tuple[str, str]] = {(p, t) for p, t in d.get("texts", [])}
        self.ids: set[str] = set(d.get("ids", []))
        self.sources: list[str] = d.get("sources", [])

    def burned(self, path: str, line: int, text: str | None = None, cid: str | None = None) -> bool:
        return ((path, line) in self.lines or (text is not None and (path, text.strip()) in self.texts)
                or (cid is not None and cid in self.ids))

    def add(self, path: str, line: int, text: str | None = None, cid: str | None = None) -> None:
        self.lines.add((path, int(line)))
        if text and text.strip():
            self.texts.add((path, text.strip()))
        if cid:
            self.ids.add(cid)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"lines": sorted(self.lines), "texts": sorted(self.texts), "ids": sorted(self.ids),
                                         "sources": self.sources}, indent=1))

    def __len__(self) -> int:
        return len(self.lines)


def burn_text(reg: Registry, repo: Path, text: str, label: str, source: str = "backend") -> int:
    """Every ``file.py:LINE`` mention. A bare file name is burned in every source file with that name (``structure.py``
    exists in two packages), with that file's current text of the line."""
    files = subprocess.run(["git", "ls-files", source], cwd=repo, capture_output=True, text=True).stdout.split()
    n = 0
    for m in _MENTION.finditer(text):
        name, line = m.group(1), int(m.group(2))
        targets = [f for f in files if f == name or f.endswith("/" + name)]
        for f in targets:
            src = (Path(repo) / f).read_text().splitlines() if (Path(repo) / f).exists() else []
            reg.add(f, line, src[line - 1] if 0 < line <= len(src) else None)
            n += 1
    reg.sources.append(f"{label}: {n} mentions")
    return n


def burn_json(reg: Registry, data, label: str) -> int:
    """Every object carrying path + line (pools, samples, stranger results), with its original line text when present."""
    n = 0

    def walk(x):
        nonlocal n
        if isinstance(x, dict):
            line = x.get("line")
            if not isinstance(line, int) and isinstance(x.get("start"), list) and x["start"] and isinstance(x["start"][0], int):
                line = x["start"][0]  # a cosmic-ray candidate: start = [line, col]
            if isinstance(x.get("path"), str) and x["path"].endswith(".py") and isinstance(line, int) and line > 0:
                reg.add(x["path"], line, x.get("original_line") or x.get("line_text"), x.get("id") or x.get("candidate_id"))
                n += 1
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    reg.sources.append(f"{label}: {n} entries")
    return n


def burn_git_log(reg: Registry, repo: Path, source: str = "backend") -> int:
    log = subprocess.run(["git", "log", "--format=%B"], cwd=repo, capture_output=True, text=True).stdout
    return burn_text(reg, repo, log, "commit messages", source=source)
