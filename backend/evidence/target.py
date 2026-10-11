"""Phase 3.6 orchestration: explicit target repositories, an account, or both.

    analyze_targets(["owner/repo", "https://github.com/o/r/tree/main/src"], user="login")

Every target gets a status. One bad target never stops the batch, and every
numeric field of a target that was not analysed stays None. The report
echoes an input_spec so it can explain itself without the command line.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field


from backend.contracts import (AnalysisReport, ExcludedFile, HistoryFacts, InputSpec, RepoFacts, Structure,
                               TargetReport)

from . import cache, config
from . import question_surface as qs
from .authorship import compute_authorship, line_authorship
from .clone import cloned_repo
from .degraded import degraded_authorship, degraded_facts
from .errors import (GitError, GitHubAuthError, GitHubError, GitHubForbidden, GitHubNetworkError, GitHubNotFound,
                     GitHubRateLimited, GitTimeout)
from .exclusions import REASONS
from .github_client import GitHubClient
from .gitutil import run_git
from .identity import IdentitySet, build_identity_set
from .resolve import NAME_RE, Target, resolve_target, settle_ref
from .snapshot import build_snapshot

log = logging.getLogger(__name__)
MAX_TOP_EXCLUDED = 10


@dataclass
class Options:
    max_targets: int = config.MAX_TARGETS_PER_RUN
    max_repo_mb: int = config.MAX_REPO_MB
    clone_timeout_s: int = config.CLONE_TIMEOUT_S
    analysis_timeout_s: int = config.ANALYSIS_TIMEOUT_S
    use_cache: bool = True
    path_override: str | None = None
    ref_override: str | None = None
    allow_local: bool = False  # tests only: clone from local paths
    clone_url_for: dict[str, str] = field(default_factory=dict)  # tests only: full_name -> local path


def _report(t: Target, status: str, *, ref_requested: str | None = None, ref_analyzed: str | None = None,
            **kw) -> TargetReport:
    """``ref_requested`` defaults to the ref the input named; ``ref_analyzed``
    stays None unless something was actually analysed."""
    requested = ref_requested if ref_requested is not None else t.ref
    return TargetReport(raw_input=t.raw_input, target=t.model_dump(), status=status,  # type: ignore[arg-type]
                        ref_requested=requested, ref_analyzed=ref_analyzed, **kw)


def _ref_check(t: Target, requested: str | None, analyzed: str | None) -> None:
    """A parse warning whenever what was analysed is not what was asked for."""
    if requested is not None and analyzed is not None and requested != analyzed:
        t.parse_warnings.append(f"ref requested {requested!r} but ref analysed {analyzed!r}")


def _excluded_summary(excluded: list) -> tuple[dict, dict, list]:
    by_files = Counter(r for _p, _n, r in excluded)
    by_lines: Counter = Counter()
    for _p, n, r in excluded:
        by_lines[r] += n
    top = [ExcludedFile(path=p, lines=n, reason=r) for p, n, r in sorted(excluded, key=lambda e: (-e[1], e[0]))]
    return ({r: by_files.get(r, 0) for r in REASONS}, {r: by_lines.get(r, 0) for r in REASONS},
            top[:MAX_TOP_EXCLUDED])


def repo_facts_from(snap: dict, meta: dict, redirected_from: str | None, target: Target) -> RepoFacts:
    languages: Counter = Counter()
    for entry in snap.get("counted", {}).values():
        languages[entry["language"]] += entry["code"] + entry["string"]
    by_files, by_lines, top = _excluded_summary([tuple(e) for e in snap.get("excluded", [])])
    h = snap.get("history") or {}
    history = HistoryFacts(commit_count=h.get("commit_count", 0), distinct_author_count=h.get("distinct_author_count", 0),
                           first_commit=h.get("first_commit"), last_commit=h.get("last_commit"),
                           span_days=h.get("span_days", 0.0), single_commit_import=h.get("single_commit_import"),
                           committed_in_one_session=(h.get("author_span_s") or 0) < 3600,
                           committer_dates_clustered=h.get("committer_dates_clustered")) if h else None
    total = sum(languages.values())
    return RepoFacts(
        **_meta_facts(meta, redirected_from, target),
        head_sha=snap.get("head_sha"),
        history=history,
        languages=dict(languages.most_common()) if snap.get("counted") else None,
        source_files=len(snap.get("counted", {})),
        total_source_lines=total if snap.get("counted") else None,
        excluded_files_by_reason=by_files, excluded_lines_by_reason=by_lines, top_excluded_files=top,
        structure=Structure(**snap["structure"]) if snap.get("structure") else None,
        dependencies=snap.get("dependencies", []), manifests=snap.get("manifests", []),
        readme_path=snap.get("readme_path"), readme_claims=snap.get("readme_claims", []),
    )


def _meta_facts(meta: dict, redirected_from: str | None, target: Target) -> dict:
    return dict(
        full_name=meta["full_name"], redirected_from=redirected_from, default_branch=meta.get("default_branch"),
        ref_analyzed=target.ref or meta.get("default_branch"), scoped_path=target.scoped_path,
        visibility=meta.get("visibility"), archived=meta.get("archived"), size_kb=meta.get("size"),
        created_at=meta.get("created_at"), pushed_at=meta.get("pushed_at"), is_fork=meta.get("fork"),
        parent_full_name=(meta.get("parent") or {}).get("full_name") if meta.get("fork") else None,
        source_full_name=(meta.get("source") or {}).get("full_name") if meta.get("fork") else None,
    )


class _RateFloor(Exception):
    pass


def _check_rate(client: GitHubClient) -> None:
    if client.rate_limit_remaining is not None and client.rate_limit_remaining < config.RATE_LIMIT_FLOOR:
        raise _RateFloor(f"X-RateLimit-Remaining={client.rate_limit_remaining} < {config.RATE_LIMIT_FLOOR}")


def _expand(raws: list[str], user: str | None, client: GitHubClient, opts: Options,
            spec: InputSpec) -> list[Target]:
    targets: list[Target] = []
    accounts = [user] if (user and not raws) else []
    for raw in raws:
        # Behind --repo a slashless token is a repository name owned by the user,
        # never an account: the caller states the context, the string does not.
        t = resolve_target(raw, context="repo", default_owner=user)
        if t.kind == "repo":
            if opts.path_override:
                t.scoped_path, t.ref_ambiguous, t.ref_candidates = opts.path_override.strip("/"), False, []
            if opts.ref_override:
                t.ref, t.ref_ambiguous, t.ref_candidates = opts.ref_override, False, []
        spec.inputs.append({"raw": raw, "kind": t.kind, "resolved_to": t.full_name or t.owner,
                            "ref": t.ref, "scoped_path": t.scoped_path, "warnings": t.parse_warnings,
                            "invalid_reason": t.invalid_reason})
        targets.append(t)
    for account in accounts:
        from . import _select

        _check_rate(client)
        metas = client.list_repos(account)
        selected, _rest = _select(metas, opts.max_targets)
        for m in selected:
            targets.append(Target(raw_input=f"{account} (account)", kind="repo", owner=m["owner"]["login"],
                                  repo=m["name"]))
    return targets


def analyze_targets(raws: list[str], user: str | None = None, emails: list[str] | None = None,
                    opts: Options | None = None, client: GitHubClient | None = None) -> AnalysisReport:
    start = time.monotonic()
    opts = opts or Options()
    emails = [e.strip() for e in emails or [] if e.strip()]
    own = client is None
    client = client or GitHubClient()
    if emails:
        who = f"identity from {len(emails)} --email address(es)" + (
            f" (takes precedence over the username {user!r})" if user else "")
    elif user:
        who = f"identity resolved from user {user!r}"
    else:
        who = "no identity supplied: repo_facts only, authorship not computed"
    if raws:
        mode = f"explicit targets ({len(raws)}); {who}" + (
            "; the account's repositories were NOT enumerated" if user else "")
    else:
        mode = f"account mode: enumerate {user!r}'s public repositories (non-forks first); {who}"
    spec = InputSpec(user=user, extra_emails_count=len(emails), run_mode=mode)
    reports: list[TargetReport] = []
    stopped = None
    try:
        try:
            targets = _expand(raws, user, client, opts, spec)
        except _RateFloor as exc:
            targets, stopped = [], str(exc)

        # de-duplicate (case-insensitive), cap
        unique: list[Target] = []
        first_by_key: dict[str, Target] = {}
        for t in targets:
            if t.kind == "invalid":
                reports.append(_report(t, "invalid_input", message=t.invalid_reason))
                continue
            if t.key in first_by_key:
                spec.duplicates.append({"raw": t.raw_input, "duplicate_of": first_by_key[t.key].raw_input})
                reports.append(_report(t, "duplicate", message=f"same target as {first_by_key[t.key].raw_input!r}"))
                continue
            first_by_key[t.key] = t  # type: ignore[index]
            unique.append(t)
        for t in unique[opts.max_targets:]:
            reports.append(_report(t, "invalid_input", message=f"over max_targets ({opts.max_targets}); not analysed"))
        unique = unique[: opts.max_targets]

        # precheck metadata (no clone yet)
        prechecked: list[tuple[Target, dict, str | None]] = []
        for t in unique:
            if stopped:
                reports.append(_report(t, "rate_limited", message=stopped))
                continue
            try:
                _check_rate(client)
                meta, redirected = client.get_repo_meta(t.owner, t.repo)  # type: ignore[arg-type]
                if not NAME_RE.match(meta["owner"]["login"]) or not NAME_RE.match(meta["name"]):
                    raise GitHubError("API returned an unexpected repository name")
                prechecked.append((t, meta, redirected))
            except _RateFloor as exc:
                stopped = str(exc)
                reports.append(_report(t, "rate_limited", message=stopped))
            except GitHubNotFound:
                reports.append(_report(t, "not_found", http_status=404,
                                       message="repository not found (a private repository you cannot access also "
                                               "returns 404)"))
            except GitHubForbidden as exc:
                reports.append(_report(t, "access_denied", http_status=exc.status, message=str(exc)))
            except GitHubRateLimited as exc:
                stopped = f"rate limited until {exc.reset_at}"
                reports.append(_report(t, "rate_limited", http_status=403, message=stopped))
            except (GitHubAuthError, GitHubNetworkError, GitHubError) as exc:
                reports.append(_report(t, "access_denied" if isinstance(exc, GitHubAuthError) else "clone_failed",
                                       message=str(exc)))

        # identity, in precedence order: 1. --email  2. the username  3. none
        identity: IdentitySet | None = None
        unresolved = None
        if emails:
            identity = IdentitySet(login="")
            for e in emails:
                identity.add(e, "admin_supplied_email")
            spec.identity_source = "email"
        elif user and not stopped:
            try:
                _check_rate(client)
                identity = build_identity_set(user, client, repos=[m for _t, m, _r in prechecked])
                spec.identity_source = "user"
            except (_RateFloor, GitHubError) as exc:
                unresolved = f"identity for {user!r} could not be resolved: {exc}"
        if identity is None and unresolved is None:
            unresolved = "no identity supplied (give a username or --email); authorship was not computed"

        for t, meta, redirected in prechecked:
            if stopped:
                reports.append(_report(t, "rate_limited", message=stopped))
                continue
            try:
                reports.append(_analyze_one(t, meta, redirected, identity, unresolved, client, opts))
            except _RateFloor as exc:
                stopped = str(exc)
                reports.append(_report(t, "rate_limited", message=stopped))
    finally:
        if own:
            client.close()
    out = AnalysisReport(input_spec=spec, targets=reports, api_calls=client.calls_made,
                         rate_limit_remaining=client.rate_limit_remaining, stopped_reason=stopped,
                         elapsed_s=round(time.monotonic() - start, 1), exclusion_ruleset_version=cache.ruleset_version())
    out._identity = identity  # J4c: in-process only; never serialized
    return out


def identity_of(report: AnalysisReport) -> IdentitySet | None:
    """The exact identity 3.6 used for blame (J4c), for in-process callers such as 4B. Replaces rebuilding it outside
    3.6, which duplicated the precedence logic and the GitHub API calls. Not part of any serialized report."""
    return report._identity


def _analyze_one(t: Target, meta: dict, redirected: str | None, identity: IdentitySet | None, unresolved: str | None,
                 client: GitHubClient, opts: Options) -> TargetReport:
    notes: list[str] = []
    if meta.get("disabled"):
        return _report(t, "access_denied", message="repository is disabled by GitHub")
    owner, repo = meta["owner"]["login"], meta["name"]
    requested = t.ref  # None = the default branch
    if t.ref_ambiguous:
        _check_rate(client)
        branches = client.list_branches(owner, repo)
        matches = [r for r, _p in t.ref_candidates if r in set(branches)]
        # what the URL asked for: the one branch it can mean, or (no unique match)
        # its whole ref/path tail, so a fallback shows up as a mismatch
        requested = matches[0] if len(matches) == 1 else t.ref_candidates[-1][0]
        t = settle_ref(t, branches, meta.get("default_branch") or "main")
    status = "archived_ok" if meta.get("archived") else "resolved"
    size_kb = meta.get("size") or 0

    # degraded mode: too large to clone
    if size_kb > opts.max_repo_mb * 1024:
        ref = t.ref or meta.get("default_branch")
        facts = RepoFacts(**_meta_facts(meta, redirected, t))
        _check_rate(client)
        facts = degraded_facts(client, facts, owner, repo, ref, notes)
        authorship = None
        if identity is not None:
            _check_rate(client)
            authorship = degraded_authorship(client, owner, repo, ref, [identity.login] if identity.login else [],
                                             list(identity.emails))
        notes.append(f"size {size_kb / 1024:.0f} MiB (packed) over max_repo_mb {opts.max_repo_mb}: analysed from the "
                     "API only; no line-level measurement")
        _ref_check(t, requested, ref)
        facts.ref_requested = requested
        return _report(t, "too_large", mode="api_commits_only", repo_facts=facts, authorship=authorship,
                       identity_unresolved_reason=None if identity else unresolved, notes=notes,
                       ref_requested=requested, ref_analyzed=ref)

    url = opts.clone_url_for.get(meta["full_name"]) or meta["clone_url"]
    head = None
    try:
        ls = run_git(["ls-remote", "--", url, t.ref or "HEAD"], timeout=60,
                     extra_config=("-c", "protocol.file.allow=" + ("always" if opts.allow_local else "never")))
        head = ls.split(b"\t", 1)[0].decode() if ls else None
    except GitError:
        head = None
    if head is None and size_kb == 0:
        return _report(t, "empty_repo", ref_requested=requested, message="repository has no commits")
    snap = cache.load(meta["full_name"], head, t.scoped_path) if (opts.use_cache and head) else None
    cache_hit = snap is not None
    if snap is None:
        try:
            with cloned_repo(url, allow_local=opts.allow_local, ref=t.ref, timeout=opts.clone_timeout_s) as clone:
                snap = build_snapshot(clone, meta["full_name"], t.scoped_path, budget_s=opts.analysis_timeout_s)
        except GitTimeout as exc:
            return _report(t, "timeout", ref_requested=requested, message=str(exc))
        except GitError as exc:
            return _report(t, "clone_failed", ref_requested=requested, message=str(exc))
        if snap.get("empty"):
            return _report(t, "empty_repo", ref_requested=requested, message="repository has no commits")
        if opts.use_cache and snap.get("head_sha"):
            cache.save(snap, meta["full_name"], snap["head_sha"], t.scoped_path)
    notes += snap.get("notes", [])
    facts = repo_facts_from(snap, meta, redirected, t)

    # fork-aware: commits ahead of the parent
    fork_ahead, fork_error = None, None
    if meta.get("fork"):
        parent = meta.get("parent") or {}
        try:
            _check_rate(client)
            p_owner, p_repo = parent["full_name"].split("/", 1)
            cmp = client.compare(p_owner, p_repo, parent.get("default_branch") or "main",
                                 f"{owner}:{t.ref or meta.get('default_branch')}")
            facts.fork_ahead_by, facts.fork_behind_by = cmp.get("ahead_by"), cmp.get("behind_by")
            facts.fork_total_commits, facts.files_truncated = cmp.get("total_commits"), cmp.get("files_truncated")
            if cmp.get("commits_complete"):
                fork_ahead = {c["sha"] for c in cmp.get("commits", [])}
            else:
                fork_error = (f"compare returned {len(cmp.get('commits', []))} of {cmp.get('total_commits')} commits "
                              "ahead of the parent; the fork delta cannot be computed completely")
        except _RateFloor:
            raise
        except (KeyError, ValueError, GitHubError) as exc:
            fork_error = f"compare with the parent failed: {exc}"

    authorship = surface_lines = None
    level = reason = None
    if identity is not None:
        authorship = compute_authorship(snap, identity, owner_login=owner, is_fork=meta.get("fork"),
                                        fork_ahead=fork_ahead, fork_error=fork_error)
        level, reason = authorship.authorship_confidence, authorship.authorship_confidence_reason
        surface_lines = line_authorship(snap, identity)
    surface = qs.build(snap, surface_lines, level, reason)
    analyzed = t.ref or meta.get("default_branch")
    _ref_check(t, requested, analyzed)
    facts.ref_requested, facts.ref_analyzed = requested, analyzed
    return _report(t, status, mode="blame", repo_facts=facts, authorship=authorship,
                   identity_unresolved_reason=None if identity else unresolved, question_surface=surface,
                   notes=notes, cache_hit=cache_hit, ref_requested=requested, ref_analyzed=analyzed)



