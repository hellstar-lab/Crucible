"""Line-level authorship with ``git blame --line-porcelain``.

Blame credits the code that survives at HEAD, which is what later phases
need (evidence and bug targets must exist today). Git applies the repo's
.mailmap to the author/committer emails it reports, so they are compared
against the identity set's canonical emails.

Blank lines are excluded from every count except ``total_lines``.
Attribution is on AUTHOR email only; committer email is recorded so a
systematic author/committer divergence stays visible.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .errors import GitError, GitTimeout
from .gitutil import run_git

log = logging.getLogger(__name__)


@dataclass
class BlameLine:
    number: int  # 1-based, at HEAD
    author_email: str
    committer_email: str
    blank: bool


@dataclass
class FileBlame:
    path: str
    total_lines: int
    code_lines: int
    authored_lines: int
    authored_ranges: list[tuple[int, int]]
    committer_emails: dict[str, int]  # committer -> authored (non-blank) lines
    other_authors: dict[str, int] = field(default_factory=dict)  # non-candidate author -> non-blank lines


@dataclass
class RepoBlame:
    files: list[FileBlame] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "path: reason"

    @property
    def code_lines(self) -> int:
        return sum(f.code_lines for f in self.files)

    @property
    def authored_lines(self) -> int:
        return sum(f.authored_lines for f in self.files)

    @property
    def total_lines(self) -> int:
        return sum(f.total_lines for f in self.files)

    def other_authors(self) -> Counter:
        total: Counter = Counter()
        for f in self.files:
            total.update(f.other_authors)
        return total

    @property
    def authored_ratio(self) -> float | None:
        """None when there are no non-blank source lines (0/0 is not 0%)."""
        return self.authored_lines / self.code_lines if self.code_lines else None


def parse_line_porcelain(out: bytes) -> list[BlameLine]:
    """Parse ``git blame --line-porcelain`` output into one record per line."""
    lines: list[BlameLine] = []
    number = 0
    author = committer = ""
    for raw in out.split(b"\n"):
        if raw.startswith(b"\t"):
            text = raw[1:].decode("utf-8", errors="replace")
            lines.append(BlameLine(number, author, committer, not text.strip()))
            continue
        if raw.startswith(b"author-mail "):
            author = raw[12:].decode("utf-8", errors="replace").strip().strip("<>").lower()
        elif raw.startswith(b"committer-mail "):
            committer = raw[15:].decode("utf-8", errors="replace").strip().strip("<>").lower()
        else:
            head = raw.split(b" ")
            if len(head) >= 3 and len(head[0]) in (40, 64) and all(c in b"0123456789abcdef" for c in head[0]):
                number = int(head[2])
    return lines


def merge_ranges(authored: list[int], blank: set[int]) -> list[tuple[int, int]]:
    """Merge sorted authored line numbers into inclusive ranges, bridging gaps
    made only of blank lines (a function with blank lines inside stays whole)."""
    ranges: list[tuple[int, int]] = []
    for n in authored:
        if ranges:
            start, end = ranges[-1]
            if all(g in blank for g in range(end + 1, n)):
                ranges[-1] = (start, n)
                continue
        ranges.append((n, n))
    return ranges


def summarize(path: str, lines: list[BlameLine], is_candidate) -> FileBlame:
    blank = {ln.number for ln in lines if ln.blank}
    authored = [ln for ln in lines if not ln.blank and is_candidate(ln.author_email)]
    return FileBlame(
        path=path,
        total_lines=len(lines),
        code_lines=sum(1 for ln in lines if not ln.blank),
        authored_lines=len(authored),
        authored_ranges=merge_ranges(sorted(ln.number for ln in authored), blank),
        committer_emails=dict(Counter(ln.committer_email for ln in authored)),
        other_authors=dict(Counter(ln.author_email for ln in lines
                                   if not ln.blank and not is_candidate(ln.author_email))),
    )


def blame_file(repo: Path, path: str, is_candidate) -> FileBlame:
    out = run_git(["blame", "--line-porcelain", "HEAD", "--", path], cwd=repo, timeout=config.BLAME_TIMEOUT_S)
    return summarize(path, parse_line_porcelain(out), is_candidate)


def blame_repo(repo: Path, files: list[tuple[str, int]], is_candidate) -> RepoBlame:
    """Blame every (path, size) source file, honouring the per-file size cap,
    the file-count cap and the repo time budget. Skips are recorded, not fatal."""
    result = RepoBlame()
    todo: list[str] = []
    for i, (path, size) in enumerate(files):
        if i >= config.MAX_SOURCE_FILES_PER_REPO:
            result.skipped.append(f"{path}: over MAX_SOURCE_FILES_PER_REPO ({config.MAX_SOURCE_FILES_PER_REPO})")
        elif size > config.MAX_BLAME_FILE_BYTES:
            result.skipped.append(f"{path}: {size} bytes over MAX_BLAME_FILE_BYTES")
        else:
            todo.append(path)

    deadline = time.monotonic() + config.REPO_TIME_BUDGET_S

    def work(path: str) -> FileBlame | str:
        if time.monotonic() > deadline:
            return f"{path}: repo time budget exhausted"
        try:
            return blame_file(repo, path, is_candidate)
        except GitTimeout:
            return f"{path}: blame exceeded {config.BLAME_TIMEOUT_S}s"
        except GitError as exc:
            return f"{path}: blame failed ({exc})"

    with ThreadPoolExecutor(max_workers=config.BLAME_WORKERS) as pool:
        for item in pool.map(work, todo):
            if isinstance(item, str):
                result.skipped.append(item)
            else:
                result.files.append(item)
    result.files.sort(key=lambda f: f.path)
    return result
