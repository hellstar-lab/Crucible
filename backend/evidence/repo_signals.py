"""Deterministic repository facts. Each is a value with no judgement attached:
later phases and humans interpret them."""

from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import config
from .gitutil import run_git

DAY = 86400.0
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.jsx", "*.test.ts",
                      "*.test.tsx", "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*_spec.rb",
                      "*Test.java", "*Tests.java", "*Test.kt", "*Tests.swift", "*_test.rs", "*_test.exs")


@dataclass
class CommitFacts:
    commit_count: int
    contributor_count: int
    author_date_span_days: float
    committer_date_span_days: float
    committer_dates_clustered: bool | None  # None = not evaluated (see clustered_reason)
    clustered_reason: str | None
    largest_commit_added: int  # source lines added by the single largest commit
    commits_by_author: dict[str, int] = field(default_factory=dict)  # canonical email -> commits
    name_by_author: dict[str, str] = field(default_factory=dict)  # canonical email -> most used name


def has_tests(paths: list[str]) -> bool:
    for path in paths:
        p = PurePosixPath(path)
        if any(part in TEST_DIRS for part in p.parts[:-1]) or "src/test" in path:
            return True
        if any(fnmatch.fnmatchcase(p.name, pat) for pat in TEST_FILE_PATTERNS):
            return True
    return False


def clustered(author_times: list[int], committer_times: list[int]) -> tuple[bool | None, str | None]:
    """History-rewrite signal: rewriting (filter-branch / filter-repo) resets
    committer dates to the rewrite time while author dates keep their spread.

    Returns (value, reason). Not evaluated (None, "insufficient_history")
    when author dates span less than CLUSTER_MIN_AUTHOR_SPAN_DAYS, including
    a single commit (span 0). Otherwise clustered iff
    committer_span / author_span < CLUSTER_SPAN_FRACTION."""
    if not author_times:
        return None, "insufficient_history"
    a_span = max(author_times) - min(author_times)
    if a_span <= 0 or a_span < config.CLUSTER_MIN_AUTHOR_SPAN_DAYS * DAY:
        return None, "insufficient_history"
    c_span = max(committer_times) - min(committer_times)
    return (c_span / a_span) < config.CLUSTER_SPAN_FRACTION, None


def commit_facts(repo: Path, is_source) -> CommitFacts:
    """Facts from ``git log`` on HEAD. ``is_source(path)`` limits the
    largest-commit measurement to files that count toward authorship."""
    log_out = run_git(["log", "--format=%at %ct %aE %aN"], cwd=repo, timeout=120).decode("utf-8", "replace")
    a_times, c_times = [], []
    commits_by_author: Counter = Counter()
    names: dict[str, Counter] = {}
    for line in log_out.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        a_times.append(int(parts[0]))
        c_times.append(int(parts[1]))
        email = parts[2].strip().lower()
        commits_by_author[email] += 1
        names.setdefault(email, Counter())[parts[3].strip() if len(parts) > 3 else ""] += 1
    authors = set(commits_by_author)

    numstat = run_git(["log", "--no-renames", "--numstat", "-z", "--format=%x01%H"], cwd=repo, timeout=120)
    largest = current = 0
    for chunk in numstat.split(b"\x00"):
        for record in chunk.split(b"\n"):
            if record.startswith(b"\x01"):
                largest, current = max(largest, current), 0
                continue
            fields = record.split(b"\t")
            if len(fields) == 3 and fields[0].isdigit():
                path = fields[2].decode("utf-8", "replace")
                if is_source(path):
                    current += int(fields[0])
    largest = max(largest, current)

    span = lambda ts: (max(ts) - min(ts)) / DAY if ts else 0.0  # noqa: E731
    is_clustered, reason = clustered(a_times, c_times)
    return CommitFacts(
        commit_count=len(a_times),
        contributor_count=len(authors),
        author_date_span_days=round(span(a_times), 3),
        committer_date_span_days=round(span(c_times), 3),
        committer_dates_clustered=is_clustered,
        clustered_reason=reason,
        largest_commit_added=largest,
        commits_by_author=dict(commits_by_author),
        name_by_author={e: c.most_common(1)[0][0] for e, c in names.items()},
    )
