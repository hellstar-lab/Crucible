"""Phase 3: GitHub fetch and authorship attribution. Fully deterministic:
no model calls anywhere in this package.

    analyze_github(username, max_repos=N) -> list[RepoInfo]

For every public repository the candidate owns (non-forks first, most
recently pushed first) it clones full history, excludes files using GitHub
Linguist's own vendor/documentation data plus two of its content
heuristics, classifies every remaining line with pygount, and credits each
surviving SOURCE line (code or string, not comments or blanks) to the
candidate only when its author email belongs to their identity set.
Every exclusion is counted and reported by reason; a missing number is
preferred to a wrong one.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pygount.analysis import DuplicatePool

from backend.contracts import ExcludedFile, FileAuthorship, RepoInfo, SkippedFile, UnmatchedAuthor

from . import config
from .blame import blame_repo
from .clone import cloned_repo, dir_mb
from .errors import EvidenceError, GitError, GitHubNotFound, GitHubRateLimited, GitTimeout
from . import exclusions as ex
from .counting import FileCount, count_file
from .exclusions import GitAttributes, content_reason, path_reason
from .github_client import GitHubClient
from .gitutil import has_commits, run_git, write_blobs
from .identity import IdentitySet, build_identity_set
from .repo_signals import commit_facts, has_tests

__all__ = ["analyze_github", "analyze_github_detailed", "analyze_local_repo", "GithubAnalysis",
           "EvidenceError", "GitHubNotFound", "GitHubRateLimited"]

log = logging.getLogger(__name__)
MAX_NOTES = 20
MAX_TOP_EXCLUDED = 10
MAX_SKIPPED_FILES = 50
_STATE_REASON = {"duplicate": ex.DUPLICATE, "generated": ex.GENERATED, "empty": ex.EMPTY, "binary": ex.BINARY}


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
    return RepoInfo(name=name, language=meta.get("language"),
                    is_fork=meta.get("fork"), skip_reason=reason)


LOCAL_MACHINE = "<local-machine>"
UNCONFIGURED_EMAIL = "user.email not configured on the committing machine"
_NON_ROUTABLE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain")


def is_machine_domain(domain: str) -> bool:
    """Non-routable or hostname-derived domains (what git invents from the
    machine name when user.email is unset): .local, .lan, .home, .internal,
    .localdomain, localhost, or any domain without a dot."""
    d = domain.strip().lower().rstrip(".")
    return d == "localhost" or "." not in d or d.endswith(_NON_ROUTABLE_SUFFIXES)


def redact_email(email: str) -> str:
    """First 3 characters of the local part plus the domain ('aur...@gmail.com').
    Hostname-derived domains leak a person's name and device model while
    adding nothing, so they become '<local-machine>' ('abh...@<local-machine>')."""
    local, at, domain = email.partition("@")
    if not at:
        return f"{email[:3]}..."
    return f"{local[:3]}...@{LOCAL_MACHINE if is_machine_domain(domain) else domain}"


def email_reason(email: str) -> str | None:
    _local, at, domain = email.partition("@")
    return UNCONFIGURED_EMAIL if at and is_machine_domain(domain) else None


def unmatched_authors(other_lines: dict[str, int], commits: dict[str, int],
                      names: dict[str, str]) -> list[UnmatchedAuthor]:
    """Non-candidate authors of surviving source lines, redacted, most lines first."""
    ranked = sorted(other_lines.items(), key=lambda kv: (-kv[1], kv[0]))[: config.MAX_UNMATCHED_AUTHORS]
    return [UnmatchedAuthor(email_redacted=redact_email(email), name=names.get(email, ""),
                            authored_lines=lines, commit_count=commits.get(email, 0), reason=email_reason(email))
            for email, lines in ranked]


def _redact_counts(by_email: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for email, n in by_email.items():
        key = redact_email(email)
        out[key] = out.get(key, 0) + n
    return out


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


def _raw_line_counts(repo: Path) -> dict[str, int]:
    """Raw line count of every text file at HEAD (for reporting exclusions)."""
    try:
        out = run_git(["grep", "-I", "-c", "-z", "-e", "", "HEAD"], cwd=repo, timeout=120)
    except GitError:
        return {}
    # -z output is "HEAD:<path>\0<count>\n" repeated: each token after a NUL is
    # "<count>\n" followed by the next "HEAD:<path>".
    counts: dict[str, int] = {}
    parts = out.split(b"\x00")
    name = parts[0]
    for token in parts[1:]:
        num, _, following = token.partition(b"\n")
        if num.isdigit():
            counts[name.decode("utf-8", "replace").split(":", 1)[-1]] = int(num)
        name = following
    return counts


def _safe_dest(root: Path, rel: str) -> Path | None:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return root.joinpath(*p.parts)


def _measure(repo: Path, files: list[tuple[str, int, str]], attrs: GitAttributes, text: set[str],
             raw: dict[str, int], tmp: Path, authorship_only: list | None = None
             ) -> tuple[list[FileCount], list[tuple[str, int, str]], list[SkippedFile]]:
    """Decide every file: countable (with pygount counts) or excluded (with a reason). When ``authorship_only`` is a
    list, vendored-path code files that are authored work (exclusions.authorship_only, K3) stay excluded AND are measured
    into it, for blame only."""
    excluded: list[tuple[str, int, str]] = []
    candidates: list[tuple[str, int, str, Path]] = []
    ao_candidates: list[tuple[str, Path]] = []
    for path, size, sha in sorted(files):
        reason = path_reason(path, attrs)
        if (authorship_only is not None and reason == ex.VENDORED_PATH and ex.authorship_only(path, attrs) and 0 < size
                and size <= config.MAX_BLAME_FILE_BYTES and path in text):
            dest = _safe_dest(tmp, path)
            if dest is not None:
                ao_candidates.append((path, dest))
                write_blobs(repo, [(sha, dest)])
        if reason is None and size > 0 and path not in text:
            reason = ex.BINARY
        if reason is None and size > config.MAX_BLAME_FILE_BYTES:
            reason = ex.TOO_LARGE
        dest = _safe_dest(tmp, path) if reason is None else None
        if reason is None and dest is None:
            reason = ex.UNPARSEABLE  # unsafe path
        if reason is not None:
            excluded.append((path, raw.get(path, 0), reason))
            continue
        candidates.append((path, size, sha, dest))  # type: ignore[arg-type]

    for path, _size, _sha, _dest in candidates[config.MAX_SOURCE_FILES_PER_REPO:]:
        excluded.append((path, raw.get(path, 0), ex.TOO_LARGE))
    candidates = candidates[: config.MAX_SOURCE_FILES_PER_REPO]
    write_blobs(repo, [(sha, dest) for _p, _s, sha, dest in candidates])

    pool = DuplicatePool()
    counted: list[FileCount] = []
    skipped: list[SkippedFile] = []
    for path, _size, _sha, dest in candidates:  # sorted, so the first copy of a duplicate is the one counted
        text_content = dest.read_bytes().decode("utf-8", "replace")
        reason = content_reason(path, text_content)
        if reason is not None:
            excluded.append((path, raw.get(path, 0), reason))
            continue
        fc = count_file(dest, path, pool, root=tmp)
        if fc.counted:
            counted.append(fc)
        else:
            excluded.append((path, raw.get(path, 0), _STATE_REASON.get(fc.state, ex.UNPARSEABLE)))
            skipped.append(SkippedFile(path=path, state=fc.state, state_info=fc.state_info))
    if authorship_only is not None:
        ao_pool = DuplicatePool()
        for path, dest in ao_candidates:
            if content_reason(path, dest.read_bytes().decode("utf-8", "replace")) is not None:
                continue
            fc = count_file(dest, path, ao_pool, root=tmp)
            if fc.counted:
                authorship_only.append(fc)
    return counted, excluded, skipped


def analyze_local_repo(repo: Path, identity: IdentitySet, name: str, meta: dict | None = None) -> RepoInfo:
    """Analyse one repository already on disk (bare or not), at HEAD."""
    meta = meta or {}
    if not has_commits(repo):
        return _skipped(name, "no commits", meta)

    files = _list_files(repo)
    attrs = _gitattributes(repo)
    text = _text_paths(repo)
    raw = _raw_line_counts(repo)
    with tempfile.TemporaryDirectory(prefix="crucible-measure-") as tmp:
        counted, excluded, skipped = _measure(repo, files, attrs, text, raw, Path(tmp))

    canonical = identity.canonical_emails(repo)

    def is_candidate(email: str) -> bool:
        return email in canonical or identity.matches(email)

    blame = blame_repo(repo, counted, is_candidate)
    blamed = {f.path for f in blame.files}
    for note in blame.skipped:  # counted by pygount but not attributable: excluded, with reason
        path = note.split(": ", 1)[0]
        why = ex.TOO_LARGE if ("budget" in note or "exceeded" in note) else ex.UNPARSEABLE
        excluded.append((path, raw.get(path, 0), why))

    facts = commit_facts(repo, lambda p: p in blamed)
    measured = bool(blame.files)
    total = blame.counted("line_count") if measured else None
    share = facts.largest_commit_added / total if total else None

    notes = blame.skipped[:MAX_NOTES]
    if len(blame.skipped) > MAX_NOTES:
        notes.append(f"... and {len(blame.skipped) - MAX_NOTES} more files not attributable")
    if facts.clustered_reason:
        notes.append(f"committer_dates_clustered not evaluated: {facts.clustered_reason}")
    if not measured:
        notes.append("no countable source files after exclusions: line counts and ratio not measured")
    ratio = blame.authored_ratio if measured else None
    others = dict(blame.other_authors())
    owner = (meta.get("owner") or {}).get("login")
    owned = (owner.lower() == identity.login.lower()) if owner else None

    by_files = Counter(r for _p, _n, r in excluded)
    by_lines: Counter = Counter()
    for _p, n, r in excluded:
        by_lines[r] += n
    top_excluded = sorted(excluded, key=lambda e: (-e[1], e[0]))[:MAX_TOP_EXCLUDED]
    excluded_paths = {p for p, _n, r in excluded if r in (ex.VENDORED_PATH, ex.GENERATED)}

    return RepoInfo(
        name=name,
        authored_ratio=round(ratio, 6) if ratio is not None else None,
        builds=None,
        tests_present=has_tests([p for p, _s, _h in files if p not in excluded_paths]),
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
        source_files=len(blame.files),
        total_lines=total,
        code_lines=blame.counted("code") if measured else None,
        documentation_lines=blame.counted("documentation") if measured else None,
        string_lines=blame.counted("string") if measured else None,
        empty_lines=blame.counted("empty") if measured else None,
        source_lines=blame.source_lines if measured else None,
        authored_lines=blame.authored_lines if measured else None,
        notes=notes,
        files=[
            FileAuthorship(path=f.path, total_lines=f.count.line_count, code_lines=f.count.code,
                           documentation_lines=f.count.documentation, string_lines=f.count.string,
                           empty_lines=f.count.empty, source_lines=f.count.source_count,
                           authored_lines=f.authored_lines, authored_ranges=f.authored_ranges,
                           committer_emails=_redact_counts(f.committer_emails))
            for f in blame.files if f.authored_lines > 0
        ],
        excluded_files_by_reason={r: by_files.get(r, 0) for r in ex.REASONS},
        excluded_lines_by_reason={r: by_lines.get(r, 0) for r in ex.REASONS},
        top_excluded_files=[ExcludedFile(path=p, lines=n, reason=r) for p, n, r in top_excluded],
        skipped_files=skipped[:MAX_SKIPPED_FILES],
        unmatched_authors=unmatched_authors(others, facts.commits_by_author, facts.name_by_author),
        likely_identity_gap=identity_gap(owned, meta.get("fork"), ratio, others, blame.source_lines),
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
