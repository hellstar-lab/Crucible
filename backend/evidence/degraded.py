"""Degraded mode for repositories over the size cap: no clone, API only.

Line-level authorship is NOT measured here: every line-count and ratio
field is None, and ``mode="api_commits_only"`` says so on the report.
"""

from __future__ import annotations

import logging

from backend.contracts import Authorship, RepoFacts

from . import config
from .dependencies import manifest_parser, parse_manifests
from .errors import GitHubError
from .readme import README_NAMES, extract_claims
from .structure import detect_structure

log = logging.getLogger(__name__)
MAX_DEGRADED_MANIFESTS = 10


def degraded_facts(client, facts: RepoFacts, owner: str, repo: str, ref: str, notes: list[str]) -> RepoFacts:
    """Structure, manifests and README claims from the tree and contents APIs."""
    try:
        tree = client.get_tree(owner, repo, ref)
    except GitHubError as exc:
        log.warning("tree unavailable for %s/%s: %s", owner, repo, exc)
        return facts
    paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    facts.structure = detect_structure(paths)
    if tree.get("truncated"):
        notes.append(f"the tree API truncated the file list at {len(paths)} files; structure is partial")
    texts = {}
    for p in [p for p in paths if manifest_parser(p)][:MAX_DEGRADED_MANIFESTS]:
        try:
            texts[p] = client.get_raw(owner, repo, p, ref)
        except GitHubError:
            continue
    facts.manifests, facts.dependencies = parse_manifests(texts)
    readme = next((p for p in README_NAMES if p in set(paths)), None)
    if readme:
        try:
            facts.readme_path = readme
            facts.readme_claims = extract_claims(client.get_raw(owner, repo, readme, ref))
        except GitHubError:
            pass
    return facts


def degraded_authorship(client, owner: str, repo: str, ref: str, logins: list[str], emails: list[str]) -> Authorship:
    """Commits the API links to the candidate, plus files touched (capped)."""
    seen: dict[str, dict] = {}
    for author in (logins or []) + ([] if logins else emails):
        for c in client.list_commits(owner, repo, author=author, sha=ref):
            seen.setdefault(c["sha"], c)
    commits = sorted(seen.values(), key=lambda c: (c.get("commit") or {}).get("author", {}).get("date") or "")
    dates = [(c.get("commit") or {}).get("author", {}).get("date") for c in commits]
    dates = [d for d in dates if d]
    touched: set[str] = set()
    detail = commits[-config.DEGRADED_COMMIT_DETAIL_CAP:]
    for c in detail:
        try:
            for f in client.get_commit(owner, repo, c["sha"]).get("files") or []:
                touched.add(f["filename"])
        except GitHubError:
            continue
    n = len(commits)
    level = "none" if n <= 1 else "low"
    reason = ("Large repository analysed from the commits API only. "
              f"{n} commit(s) are linked to the candidate, but line-level authorship was not measured, so no "
              "authorship percentage exists for this repository.")
    return Authorship(
        mode="api_commits_only",
        identity_used={"emails": [], "logins": list(logins)},
        authored_ratio_meaningful=False,
        authorship_confidence=level,  # type: ignore[arg-type]
        authorship_confidence_reason=reason,
        commit_count=n,
        first_commit=dates[0] if dates else None,
        last_commit=dates[-1] if dates else None,
        files_touched=sorted(touched),
        files_touched_truncated=n > len(detail),
        commit_message_heads=[((c.get("commit") or {}).get("message") or "").split("\n", 1)[0][:100]
                              for c in reversed(commits[-100:])],
    )
