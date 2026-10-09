"""Phase 3: GitHub fetch and authorship attribution. Fully deterministic:
no model calls anywhere in this package.

    analyze_github(username, max_repos=N) -> list[RepoInfo]

For every public repository the candidate owns (non-forks first, most
recently pushed first) it clones full history, classifies files the way
GitHub Linguist does, and credits each surviving non-blank source line to
the candidate only when its author email belongs to their identity set.
Every skip is recorded with its reason; a missing number is preferred to a
wrong one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.contracts import FileAuthorship, RepoInfo, UnmatchedAuthor

from . import config
from .blame import blame_repo
from .clone import cloned_repo, dir_mb
from .errors import EvidenceError, GitError, GitHubNotFound, GitHubRateLimited, GitTimeout
from .exclusions import GitAttributes, classify
from .github_client import GitHubClient
from .gitutil import blob_heads, has_commits, run_git
from .identity import IdentitySet, build_identity_set
from .repo_signals import commit_facts, has_tests

__all__ = ["analyze_github", "analyze_github_detailed", "analyze_local_repo", "GithubAnalysis",
           "EvidenceError", "GitHubNotFound", "GitHubRateLimited"]

log = logging.getLogger(__name__)
MAX_NOTES = 20


@dataclass
class GithubAnalysis:
    username: str
    repos: list[RepoInfo]
    identity: IdentitySet | None
    api_calls: int = 0
    rate_limit_remaining: int | None = None
    authenticated: bool = False
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def _skipped(name: str, reason: str, meta: dict | None = None) -> RepoInfo:
    meta = meta or {}
    # Not measured: every numeric field stays None (never 0), see RepoInfo.
    return RepoInfo(name=name, tests_present=False, language=meta.get("language"),
                    is_fork=meta.get("fork"), skip_reason=reason)


def redact_email(email: str) -> str:
    """First 3 characters of the local part plus the domain: 'aur...@gmail.com'."""
    local, at, domain = email.partition("@")
    return f"{local[:3]}...@{domain}" if at else f"{email[:3]}..."


def unmatched_authors(other_lines: dict[str, int], commits: dict[str, int],
                      names: dict[str, str]) -> list[UnmatchedAuthor]:
    """Non-candidate authors of surviving source lines, redacted, most lines first."""
    ranked = sorted(other_lines.items(), key=lambda kv: (-kv[1], kv[0]))[: config.MAX_UNMATCHED_AUTHORS]
    return [UnmatchedAuthor(email_redacted=redact_email(email), name=names.get(email, ""),
                            authored_lines=lines, commit_count=commits.get(email, 0))
            for email, lines in ranked]


def identity_gap(owned: bool | None, is_fork: bool | None, ratio: float | None,
                 other_lines: dict[str, int], code_lines: int) -> bool | None:
    """The data shape of "same person, unlinked git identity". None when
    ownership or fork status is unknown."""
    if owned is None or is_fork is None:
        return None
    top = max(other_lines.values(), default=0)
    return bool(owned and not is_fork and ratio == 0.0 and code_lines
                and top / code_lines > config.IDENTITY_GAP_SHARE)


def _list_files(repo: Path) -> list[tuple[str, int, str]]:
    """(path, size, blob sha) for every regular file at HEAD (no symlinks/submodules)."""
    out = run_git(["ls-tree", "-r", "-l", "-z", "HEAD"], cwd=repo, timeout=120)
    files = []
    for entry in out.split(b"\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode, kind, sha, size = meta.split()
        if kind != b"blob" or mode == b"120000" or size == b"-":
            continue
        files.append((path.decode("utf-8", "replace"), int(size), sha.decode()))
    return files


def _text_paths(repo: Path) -> set[str]:
    """Paths git considers text (``git grep -I`` skips binaries)."""
    try:
        out = run_git(["grep", "-I", "--name-only", "-z", "-e", "", "HEAD"], cwd=repo, timeout=120)
    except GitError:
        return set()  # git grep exits 1 when nothing matches
    return {p.decode("utf-8", "replace").split(":", 1)[1] for p in out.split(b"\x00") if p}


def _gitattributes(repo: Path) -> GitAttributes:
    try:
        return GitAttributes.parse(run_git(["show", "HEAD:.gitattributes"], cwd=repo, timeout=30).decode("utf-8", "replace"))
    except GitError:
        return GitAttributes()


def analyze_local_repo(repo: Path, identity: IdentitySet, name: str, meta: dict | None = None) -> RepoInfo:
    """Analyse one repository already on disk (bare or not), at HEAD."""
    meta = meta or {}
    if not has_commits(repo):
        return _skipped(name, "no commits", meta)

    files = _list_files(repo)
    attrs = _gitattributes(repo)
    text = _text_paths(repo)
    heads = blob_heads(repo, [sha for _p, size, sha in files if 0 < size <= config.MAX_BLAME_FILE_BYTES])
    categories = {
        path: classify(path, attrs, head=heads.get(sha), is_binary=(size > 0 and path not in text))
        for path, size, sha in files
    }
    sources = [(path, size) for path, size, _sha in files if categories[path] == "source"]
    source_set = {p for p, _ in sources}

    canonical = identity.canonical_emails(repo)

    def is_candidate(email: str) -> bool:
        return email in canonical or identity.matches(email)

    blame = blame_repo(repo, sources, is_candidate)
    facts = commit_facts(repo, lambda p: p in source_set)
    share = facts.largest_commit_added / blame.total_lines if blame.total_lines else None

    notes = blame.skipped[:MAX_NOTES]
    if len(blame.skipped) > MAX_NOTES:
        notes.append(f"... and {len(blame.skipped) - MAX_NOTES} more files skipped")
    if facts.clustered_reason:
        notes.append(f"committer_dates_clustered not evaluated: {facts.clustered_reason}")
    ratio = blame.authored_ratio
    others = dict(blame.other_authors())
    owner = (meta.get("owner") or {}).get("login")
    owned = (owner.lower() == identity.login.lower()) if owner else None

    return RepoInfo(
        name=name,
        authored_ratio=round(ratio, 6) if ratio is not None else None,
        builds=None,
        tests_present=has_tests([p for p, c in categories.items() if c not in ("vendored", "generated")]),
        commit_span_days=int(facts.author_date_span_days),
        language=meta.get("language"),
        is_fork=meta.get("fork"),
        commit_count=facts.commit_count,
        contributor_count=facts.contributor_count,
        single_commit_import=(share > config.SINGLE_COMMIT_IMPORT_RATIO) if share is not None else None,
        largest_commit_share=round(share, 6) if share is not None else None,
        author_date_span_days=facts.author_date_span_days,
        committer_date_span_days=facts.committer_date_span_days,
        committer_dates_clustered=facts.committer_dates_clustered,
        source_files=len(sources),
        total_lines=blame.total_lines,
        code_lines=blame.code_lines,
        authored_lines=blame.authored_lines,
        notes=notes,
        files=[
            FileAuthorship(path=f.path, total_lines=f.total_lines, code_lines=f.code_lines,
                           authored_lines=f.authored_lines, authored_ranges=f.authored_ranges,
                           committer_emails=f.committer_emails)
            for f in blame.files if f.authored_lines > 0
        ],
        unmatched_authors=unmatched_authors(others, facts.commits_by_author, facts.name_by_author),
        likely_identity_gap=identity_gap(owned, meta.get("fork"), ratio, others, blame.code_lines),
    )


def _select(repos: list[dict], max_repos: int) -> tuple[list[dict], list[dict]]:
    """Non-forks first, then forks; each group most recently pushed first."""
    order = sorted(repos, key=lambda r: (bool(r.get("fork")), -(_ts(r.get("pushed_at")))))
    return order[:max_repos], order[max_repos:]


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def analyze_github_detailed(username: str, max_repos: int = config.MAX_REPOS,
                            client: GitHubClient | None = None) -> GithubAnalysis:
    start = time.monotonic()
    own = client is None
    client = client or GitHubClient()
    try:
        client.get_user(username)  # raises GitHubNotFound for an unknown user
        selected, rest = _select(client.list_repos(username), max_repos)
        identity = build_identity_set(username, client, repos=selected)
        results: list[RepoInfo] = []
        for meta in selected:
            name = meta["name"]
            if (meta.get("size") or 0) > config.MAX_REPO_MB * 1024:
                results.append(_skipped(name, f"API size {meta['size'] / 1024:.0f} MB over MAX_REPO_MB", meta))
                continue
            try:
                with cloned_repo(meta["clone_url"]) as repo:
                    size = dir_mb(repo)
                    if size > config.MAX_REPO_MB:
                        results.append(_skipped(name, f"cloned size {size:.0f} MB over MAX_REPO_MB", meta))
                        continue
                    results.append(analyze_local_repo(repo, identity, name, meta))
            except GitTimeout:
                results.append(_skipped(name, f"clone exceeded CLONE_TIMEOUT_S ({config.CLONE_TIMEOUT_S}s)", meta))
            except GitError as exc:
                results.append(_skipped(name, f"git failed: {exc}", meta))
        results.extend(_skipped(m["name"], f"beyond max_repos ({max_repos})", m) for m in rest)
        return GithubAnalysis(username, results, identity, client.calls_made, client.rate_limit_remaining,
                              client.authenticated, time.monotonic() - start)
    finally:
        if own:
            client.close()


def analyze_github(username: str, max_repos: int = config.MAX_REPOS) -> list[RepoInfo]:
    """Populates ``EvidenceReport.repositories``."""
    return analyze_github_detailed(username, max_repos).repos
