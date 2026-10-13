"""The four actions, shared by the participant's session and the AI stranger, so their affordances are identical by
construction: FILE_OPEN, SEARCH, TEST_RUN, ANSWER. Both start with the same view: the repository's file tree and the
failing test's name and output. Nothing else: no diff, no hint, no module name.

Sandbox: every path is resolved inside the worktree; dotfiles and dot-directories (.env, .git, ...) are never listed,
read or searched.

Visible file set (``hide``): directories withheld from a session, ONE implementation for the participant and the
stranger. A hidden path is not listed, opened, searched or run, exactly like a dotfile. The named conditions:
  code   docs/ hidden: the hunt from the code (and its docstrings) alone;
  docs   nothing hidden: the repository's prose is available too."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ACTIONS = ("FILE_OPEN", "SEARCH", "TEST_RUN", "ANSWER")
READ_LINES = 250
OUT_CHARS = 6000
MAX_SEARCH_HITS = 60


CONDITIONS: dict[str, tuple[str, ...]] = {"code": ("docs",), "docs": ()}


def hidden_for(condition: str | None) -> tuple[str, ...]:
    if condition is None:
        return ()
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}; one of {sorted(CONDITIONS)}")
    return CONDITIONS[condition]


def _hidden(rel: str, hide: tuple[str, ...] = ()) -> bool:
    parts = Path(rel).parts
    if any(part.startswith(".") for part in parts):
        return True
    posix = Path(rel).as_posix().strip("/")
    return any(posix == h.strip("/") or posix.startswith(h.strip("/") + "/") for h in hide)


def safe_path(wt: Path, rel: str | None, hide: tuple[str, ...] = ()) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    if _hidden(rel, hide):
        return None
    root = Path(wt).resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        return None
    return None if p != root and _hidden(p.relative_to(root).as_posix(), hide) else p  # app/../docs is still docs


def tree(wt: Path, hide: tuple[str, ...] = ()) -> list[str]:
    """Tracked files, minus anything hidden. What the participant sees as 'the repo'."""
    out = subprocess.run(["git", "ls-files"], cwd=wt, capture_output=True, text=True).stdout.split("\n")
    return [f for f in out if f and not _hidden(f, hide)]


def file_open(wt: Path, path: str, start: int | None = None, hide: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """(text shown, relative path opened or None)."""
    p = safe_path(wt, path, hide)
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


def search(wt: Path, pattern: str, path: str | None = None, hide: tuple[str, ...] = ()) -> str:
    """grep -n over tracked, non-hidden text files (regex; a pattern that does not compile is searched literally)."""
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    scope = (path or "").strip().strip("/")
    if scope and safe_path(wt, scope, hide) is None:
        return "error: not a directory or file in the repository"
    hits = []
    for rel in tree(wt, hide):
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


def test_run(wt: Path, selector: str | None = None, python: str | None = None, timeout_s: int = 900,
             hide: tuple[str, ...] = ()) -> str:
    sel = (selector or "").strip()
    if sel.startswith(("-", "/")) or ".." in sel or _hidden(sel.split("::")[0], hide):
        return "error: give a pytest selector (a test path), not options"
    try:
        r = subprocess.run([python or sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *([sel] if sel else [])],
                           cwd=wt, capture_output=True, text=True, timeout=timeout_s)
        return (r.stdout + r.stderr)[-OUT_CHARS:]
    except subprocess.TimeoutExpired:
        return f"error: the test run timed out after {timeout_s} s"


test_run.__test__ = False  # not a pytest test


UNBOUNDED = "unbounded"


def statement_span(text: str, line: int) -> tuple[int, int]:
    """The lines of the innermost statement containing ``line``. A compound statement's HEADER (``if a < b:`` up to its
    first body line) is its own span, so an answer inside the if-body is not a hit for a mutation in the condition.
    Falls back to the line itself (unparseable text, a bare ``else:``/``finally:`` line, a decorator)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return line, line

    def blocks(node):
        out = [getattr(node, f, None) or [] for f in ("body", "orelse", "finalbody")]
        out += [h.body for h in getattr(node, "handlers", None) or []]
        out += [c.body for c in getattr(node, "cases", None) or []]
        return [b for b in out if b]

    def find(stmts):
        for node in stmts:
            if not (node.lineno <= line <= (node.end_lineno or node.lineno)):
                continue
            kids = blocks(node)
            for b in kids:
                hit = find(b)
                if hit:
                    return hit
            for h in (getattr(node, "handlers", None) or []):  # an except header
                if h.lineno <= line < (h.body[0].lineno if h.body else h.lineno + 1):
                    return h.lineno, (h.body[0].lineno - 1 if h.body else h.lineno)
            if kids:
                first = min(b[0].lineno for b in kids)
                return (node.lineno, first - 1) if line < first else (line, line)
            return node.lineno, node.end_lineno or node.lineno
        return None

    return find(tree.body) or (line, line)


def correct(candidate: dict, path: str | None, line, span: tuple[int, int] | None = None) -> bool:
    """A hit at STATEMENT granularity: any line of the mutated statement (``span``, from statement_span on the planted
    file). Without a span, the exact mutated line(s)."""
    if path != candidate["path"] or not isinstance(line, int):
        return False
    lo, hi = span or (candidate["line"], candidate["end_line"])
    return lo <= line <= hi


def localisation_cost(log: list[dict], path: str, found: bool):
    """Actions between FIRST OPENING the correct file and answering (the answer counts, the opening does not), or
    "unbounded" when the run never localised the bug: it never answered correctly within its budget."""
    if not found:
        return UNBOUNDED
    answer_at = next(i for i, e in enumerate(log, 1) if e["type"] == "ANSWER")
    opened_at = next((i for i, e in enumerate(log, 1) if e["type"] == "FILE_OPEN" and e.get("path") == path and e.get("ok", True)), None)
    if opened_at is None:  # answered correctly without ever opening the file (from search output): cost from the reveal
        opened_at = next((i for i, e in enumerate(log, 1) if e["type"] == "SEARCH" and path in (e.get("files_hit") or [])), answer_at)
    return answer_at - opened_at
