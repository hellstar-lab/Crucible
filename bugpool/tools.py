"""The four actions, shared by the participant's session and the AI stranger, so their affordances are identical by
construction: FILE_OPEN, SEARCH, TEST_RUN, ANSWER. Both start with the same view: the repository's file tree and the
failing test's name and output. Nothing else: no diff, no hint, no module name.

Sandbox: every path is resolved inside the worktree; dotfiles and dot-directories (.env, .git, ...) are never listed,
read or searched."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ACTIONS = ("FILE_OPEN", "SEARCH", "TEST_RUN", "ANSWER")
READ_LINES = 250
OUT_CHARS = 6000
MAX_SEARCH_HITS = 60


def _hidden(rel: str) -> bool:
    return any(part.startswith(".") for part in Path(rel).parts)


def safe_path(wt: Path, rel: str | None) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    if _hidden(rel):
        return None
    p = (Path(wt) / rel).resolve()
    return p if p.is_relative_to(Path(wt).resolve()) else None


def tree(wt: Path) -> list[str]:
    """Tracked files, minus anything hidden. What the participant sees as 'the repo'."""
    out = subprocess.run(["git", "ls-files"], cwd=wt, capture_output=True, text=True).stdout.split("\n")
    return [f for f in out if f and not _hidden(f)]


def file_open(wt: Path, path: str, start: int | None = None) -> tuple[str, str | None]:
    """(text shown, relative path opened or None)."""
    p = safe_path(wt, path)
    if p is None or not p.is_file():
        return "error: not a readable file in the repository", None
    try:
        lines = p.read_text().splitlines()
    except UnicodeDecodeError:
        return "error: binary file", None
    s = max(1, start or 1)
    chunk = lines[s - 1: s - 1 + READ_LINES]
    rel = p.relative_to(Path(wt).resolve()).as_posix()
    return f"{rel} lines {s}-{s + len(chunk) - 1} of {len(lines)}\n" + "\n".join(f"{s + i:>5}| {ln}" for i, ln in enumerate(chunk)), rel


def search(wt: Path, pattern: str, path: str | None = None) -> str:
    """grep -n over tracked, non-hidden text files (regex; a pattern that does not compile is searched literally)."""
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    scope = (path or "").strip().strip("/")
    if scope and safe_path(wt, scope) is None:
        return "error: not a directory or file in the repository"
    hits = []
    for rel in tree(wt):
        if scope and not (rel == scope or rel.startswith(scope + "/")):
            continue
        try:
            text = (Path(wt) / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= MAX_SEARCH_HITS:
                    return "\n".join(hits) + f"\n... (first {MAX_SEARCH_HITS} matches)"
    return "\n".join(hits) or "no matches"


def test_run(wt: Path, selector: str | None = None, python: str | None = None, timeout_s: int = 900) -> str:
    sel = (selector or "").strip()
    if sel.startswith(("-", "/")) or ".." in sel or _hidden(sel.split("::")[0]):
        return "error: give a pytest selector (a test path), not options"
    try:
        r = subprocess.run([python or sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *([sel] if sel else [])],
                           cwd=wt, capture_output=True, text=True, timeout=timeout_s)
        return (r.stdout + r.stderr)[-OUT_CHARS:]
    except subprocess.TimeoutExpired:
        return f"error: the test run timed out after {timeout_s} s"


test_run.__test__ = False  # not a pytest test


def correct(candidate: dict, path: str | None, line) -> bool:
    return path == candidate["path"] and isinstance(line, int) and candidate["line"] <= line <= candidate["end_line"]
