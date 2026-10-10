"""Identity-dependent attribution, computed from an identity-free snapshot.

Also the history-absence confidence ceiling. A single-commit import and a
fork are the same problem in different clothes: code the candidate may not
have written, attributed to them. A fork is detectable by API; a re-upload
is not, so only history can bound how much an authorship number can mean.
"""

from __future__ import annotations

import re
from collections import Counter

from backend.contracts import Authorship, CommitInfo, HotFile

from .identity import IdentitySet

MAX_HOT_FILES = 20
MAX_COMMITS = 50
HOUR = 3600

_MAILMAP_EMAILS = re.compile(r"<([^>]*)>")


# --------------------------------------------------------------------------
# mailmap (snapshot-only: no clone needed, so cached snapshots work)
# --------------------------------------------------------------------------


def parse_mailmap(text: str) -> list[tuple[str | None, str, str | None]]:
    """Entries as (proper_email or None, commit_email, commit_name or None),
    following gitmailmap(5). Lines with a single <email> only rename."""
    entries = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        emails = _MAILMAP_EMAILS.findall(line)
        if len(emails) == 1:
            continue  # "Proper Name <commit@email>": name change only
        if len(emails) >= 2:
            after_first = line.split(">", 1)[1]
            commit_name = after_first.split("<", 1)[0].strip() or None
            entries.append((emails[0].strip().lower() or None, emails[1].strip().lower(), commit_name))
    return entries


def canonical_identity_emails(identity: IdentitySet, mailmap_text: str) -> set[str]:
    emails = set(identity.emails)
    if identity.user_id is not None and identity.login:
        emails.add(f"{identity.user_id}+{identity.login}@users.noreply.github.com".lower())
    if identity.login:
        emails.add(f"{identity.login}@users.noreply.github.com".lower())
    names = {n for ns in identity.names.values() for n in ns}
    out = set(emails)
    for proper, commit_email, commit_name in parse_mailmap(mailmap_text):
        if proper and commit_email in emails and (commit_name is None or commit_name in names):
            out.add(proper)
    return out


# --------------------------------------------------------------------------
# confidence ceiling
# --------------------------------------------------------------------------


def authorship_confidence(history: dict) -> tuple[str, str]:
    """(level, reason) where reason is readable by a hiring manager."""
    n = history.get("commit_count") or 0
    authors = history.get("distinct_author_count") or 0
    span_s = history.get("author_span_s") or 0
    days = history.get("span_days") or 0.0
    if n <= 1:
        return "none", ("This repository has one commit and no history. Blame attributes every line to the "
                        "committer because there is nothing to compare against. Authorship cannot be established "
                        "from version history.")
    if authors == 1 and span_s < HOUR:
        return "none", (f"All {n} commits are by one author within {max(1, span_s // 60)} minutes. Blame "
                        "attributes every line to that committer by construction, so the history does not show who "
                        "wrote the code. Authorship cannot be established from version history.")
    if n < 5:
        return "low", (f"Only {n} commits. Most lines arrived in a few large changes, so version history says "
                       "little about who wrote them.")
    if days < 1:
        return "low", (f"The whole history spans under one day ({n} commits). That is consistent with code "
                       "being imported or re-uploaded rather than written over time.")
    if history.get("committer_dates_clustered"):
        return "low", ("Committer dates are clustered into under 1% of the author-date span, the pattern left "
                       "by rewriting history. Author dates may not be original.")
    if authors <= 1:
        return "medium", (f"{n} commits over {days:.0f} days, all by one author. The history is real, but there "
                          "is no other contributor to compare against.")
    if days >= 7:
        return "high", f"{n} commits by {authors} authors over {days:.0f} days."
    return "medium", f"{n} commits by {authors} authors, but over only {days:.1f} days."


# --------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------


def _redact(email: str) -> str:
    from . import redact_email

    return redact_email(email)


def compute_authorship(snap: dict, identity: IdentitySet, *, owner_login: str | None, is_fork: bool | None,
                       fork_ahead: set[str] | None = None, fork_error: str | None = None) -> Authorship:
    from . import identity_gap, unmatched_authors

    canonical = canonical_identity_emails(identity, snap.get("mailmap", ""))

    def is_candidate(email: str) -> bool:
        return email in canonical or identity.matches(email)

    commits = snap.get("commits", [])
    cand_commit = [is_candidate(c["author_email"]) for c in commits]
    total = authored = 0
    delta_total = delta_authored = 0
    others: Counter = Counter()
    hot: list[HotFile] = []
    for path, entry in snap.get("counted", {}).items():
        f_total = f_auth = 0
        for mark, ci in zip(entry["marks"], entry["line_commits"]):
            if mark not in "cs":
                continue
            f_total += 1
            mine = cand_commit[ci]
            if mine:
                f_auth += 1
            else:
                others[commits[ci]["author_email"]] += 1
            if fork_ahead is not None and commits[ci]["sha"] in fork_ahead:
                delta_total += 1
                delta_authored += int(mine)
        total += f_total
        authored += f_auth
        if f_auth:
            hot.append(HotFile(path=path, authored_lines=f_auth, total_lines=f_total, ratio=round(f_auth / f_total, 4)))

    level, reason = authorship_confidence(snap.get("history") or {})
    whole = round(authored / total, 6) if total else None
    ratio_whole = ratio_delta = None
    delta_reason = None
    headline = whole
    if is_fork:
        if fork_error is not None or fork_ahead is None:
            delta_reason = fork_error or "fork comparison unavailable"
            headline = None  # never substitute the whole-repo number for the fork delta
        else:
            ratio_whole = whole
            ratio_delta = round(delta_authored / delta_total, 6) if delta_total else None
            if not delta_total:
                delta_reason = "no surviving source lines come from commits ahead of the parent"
            headline = ratio_delta

    commits_by = Counter(c["author_email"] for c in commits if c.get("author_time"))
    names = {c["author_email"]: c["author_name"] for c in commits if c.get("author_name")}
    mine = sorted((c for c, m in zip(commits, cand_commit) if m and c.get("author_time")),
                  key=lambda c: -c["author_time"])[:MAX_COMMITS]
    from datetime import datetime, timezone

    owned = (owner_login.lower() == identity.login.lower()) if owner_login and identity.login else None
    return Authorship(
        mode="blame",
        identity_used={"emails": sorted(_redact(e) for e in canonical), "logins": [identity.login] if identity.login else []},
        authored_source_lines=authored if total else None,
        total_source_lines=total if total else None,
        authored_ratio=headline,
        authored_ratio_whole_repo=ratio_whole,
        authored_ratio_fork_delta=ratio_delta,
        fork_delta_source_lines=delta_total if (is_fork and fork_ahead is not None) else None,
        fork_delta_reason=delta_reason,
        authored_ratio_meaningful=level != "none" and headline is not None,
        authorship_confidence=level,  # type: ignore[arg-type]
        authorship_confidence_reason=reason,
        unmatched_authors=unmatched_authors(dict(others), dict(commits_by), names),
        likely_identity_gap=identity_gap(owned, is_fork, whole, dict(others), total),
        hot_files=sorted(hot, key=lambda h: (-h.authored_lines, h.path))[:MAX_HOT_FILES],
        commits=[CommitInfo(sha=c["sha"], authored_date=datetime.fromtimestamp(c["author_time"], timezone.utc)
                            .isoformat(timespec="seconds"), message_head=c["message_head"],
                            files_changed=c["files_changed"], insertions=c["insertions"], deletions=c["deletions"])
                 for c in mine],
    )


def line_authorship(snap: dict, identity: IdentitySet) -> dict[str, list[tuple[bool, bool]]]:
    """Per counted file, per line: (is_source, authored_by_candidate)."""
    canonical = canonical_identity_emails(identity, snap.get("mailmap", ""))
    commits = snap.get("commits", [])
    cand = [c["author_email"] in canonical or identity.matches(c["author_email"]) for c in commits]
    return {path: [(m in "cs", cand[ci]) for m, ci in zip(e["marks"], e["line_commits"])]
            for path, e in snap.get("counted", {}).items()}

