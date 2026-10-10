"""Line-level authorship with ``git blame --line-porcelain``.

Blame credits the code that survives at HEAD, which is what later phases
need (evidence and bug targets must exist today). Git applies the repo's
.mailmap to the author/committer emails it reports, so they are compared
against the identity set's canonical emails.

Only SOURCE lines count: those pygount marks as code or string (see
counting.py). Comments, docstrings and blank lines are not evidence of
engineering. Attribution is on AUTHOR email only; committer email is
recorded so a systematic author/committer divergence stays visible.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .counting import SOURCE_MARKS, FileCount
from .errors import GitError, GitTimeout
from .gitutil import run_git

log = logging.getLogger(__name__)


@dataclass
class BlameLine:
    number: int  # 1-based, at HEAD
    author_email: str
    committer_email: str
    blank: bool
    sha: str = ""  # commit that last touched the line


@dataclass
class FileBlame:
    path: str
    count: FileCount  # pygount's per-file counts (code / documentation / string / empty)
    authored_lines: int  # candidate-authored SOURCE lines (code + string)
    authored_ranges: list[tuple[int, int]]
    committer_emails: dict[str, int]  # committer -> authored source lines
    other_authors: dict[str, int] = field(default_factory=dict)  # non-candidate author -> source lines

    @property
    def total_lines(self) -> int:
        return self.count.line_count

    @property
    def source_lines(self) -> int:
        return self.count.source_count


@dataclass
class RepoBlame:
    files: list[FileBlame] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "path: reason"

    @property
    def source_lines(self) -> int:
        return sum(f.source_lines for f in self.files)

    def counted(self, attr: str) -> int:
        return sum(getattr(f.count, attr) for f in self.files)

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
        """Authored source lines / source lines. None when there are no
        source lines at all (0/0 is "not measured", not 0%)."""
        return self.authored_lines / self.source_lines if self.source_lines else None


def parse_line_porcelain(out: bytes) -> list[BlameLine]:
    """Parse ``git blame --line-porcelain`` output into one record per line."""
    lines: list[BlameLine] = []
    number = 0
    author = committer = sha = ""
    for raw in out.split(b"\n"):
        if raw.startswith(b"\t"):
            text = raw[1:].decode("utf-8", errors="replace")
            lines.append(BlameLine(number, author, committer, not text.strip(), sha))
            continue
        if raw.startswith(b"author-mail "):
            author = raw[12:].decode("utf-8", errors="replace").strip().strip("<>").lower()
        elif raw.startswith(b"committer-mail "):
            committer = raw[15:].decode("utf-8", errors="replace").strip().strip("<>").lower()
        else:
            head = raw.split(b" ")
            if len(head) >= 3 and len(head[0]) in (40, 64) and all(c in b"0123456789abcdef" for c in head[0]):
                number = int(head[2])
                sha = head[0].decode()
    return lines


def merge_ranges(authored: list[int], bridgeable: set[int]) -> list[tuple[int, int]]:
    """Merge sorted authored line numbers into inclusive ranges, bridging gaps
    made only of non-source lines (blank, comments, docstrings), so a function
    with comments inside stays whole."""
    ranges: list[tuple[int, int]] = []
    for n in authored:
        if ranges:
            start, end = ranges[-1]
            if all(g in bridgeable for g in range(end + 1, n)):
                ranges[-1] = (start, n)
                continue
        ranges.append((n, n))
    return ranges


class AlignmentError(ValueError):
    """Blame and pygount disagree on a file's line count (e.g. lone CR line endings)."""


def summarize(count: FileCount, lines: list[BlameLine], is_candidate) -> FileBlame:
    """Combine blame (who wrote each line) with pygount marks (what each line is)."""
    marks = list(count.marks)
    extra = lines[len(marks):]
    if len(lines) > len(marks) and all(ln.blank for ln in extra):
        marks += ["e"] * len(extra)  # trailing blank lines that pygount's splitter drops
    if len(lines) != len(marks):
        raise AlignmentError(f"blame has {len(lines)} lines, pygount {len(count.marks)}")
    source = {i + 1 for i, m in enumerate(marks) if m in SOURCE_MARKS}
    non_source = {i + 1 for i, m in enumerate(marks) if m not in SOURCE_MARKS}
    authored = [ln for ln in lines if ln.number in source and is_candidate(ln.author_email)]
    return FileBlame(
        path=count.path,
        count=count,
        authored_lines=len(authored),
        authored_ranges=merge_ranges(sorted(ln.number for ln in authored), non_source),
        committer_emails=dict(Counter(ln.committer_email for ln in authored)),
        other_authors=dict(Counter(ln.author_email for ln in lines
                                   if ln.number in source and not is_candidate(ln.author_email))),
    )


def blame_file(repo: Path, count: FileCount, is_candidate) -> FileBlame:
    out = run_git(["blame", "--line-porcelain", "HEAD", "--", count.path], cwd=repo, timeout=config.BLAME_TIMEOUT_S)
    return summarize(count, parse_line_porcelain(out), is_candidate)


def blame_repo(repo: Path, counts: list[FileCount], is_candidate) -> RepoBlame:
    """Blame every counted file within the repo time budget. Skips are
    recorded as "path: reason", never fatal."""
    result = RepoBlame()
    deadline = time.monotonic() + config.REPO_TIME_BUDGET_S

    def work(count: FileCount) -> FileBlame | str:
        if time.monotonic() > deadline:
            return f"{count.path}: repo time budget exhausted"
        try:
            return blame_file(repo, count, is_candidate)
        except AlignmentError as exc:
            return f"{count.path}: not counted, {exc}"
        except GitTimeout:
            return f"{count.path}: blame exceeded {config.BLAME_TIMEOUT_S}s"
        except GitError as exc:
            return f"{count.path}: blame failed ({exc})"

    with ThreadPoolExecutor(max_workers=config.BLAME_WORKERS) as pool:
        for item in pool.map(work, counts):
            if isinstance(item, str):
                result.skipped.append(item)
            else:
                result.files.append(item)
    result.files.sort(key=lambda f: f.path)
    return result
