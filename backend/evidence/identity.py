"""Which git identities belong to the candidate.

Matching is on EMAIL only, never on name: names are free text and
collide. Three signals, in priority order, each recorded as evidence so a
human can audit why an email was accepted:

  1. github_commit_login  GitHub's commits API linked the commit to the
                          candidate's login (authoritative).
  2. noreply              a GitHub noreply address embedding the login
                          (ID+login@users.noreply.github.com, or the older
                          login@users.noreply.github.com).
  3. profile_email        the public email on the candidate's profile.

A repository's .mailmap is applied by git itself (blame and log report
canonical emails); ``canonical_emails`` maps the identity set through the
same mailmap so aliases unify.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import GitError, GitHubError, GitHubNotFound
from .gitutil import run_git

log = logging.getLogger(__name__)

MAX_EVIDENCE_PER_SIGNAL = 3  # sample commits kept per email per signal, for audit
NOREPLY_RE = re.compile(r"^(?:(?P<id>\d+)\+)?(?P<login>[A-Za-z0-9-]+)@users\.noreply\.github\.com$", re.IGNORECASE)


def parse_noreply(email: str) -> tuple[int | None, str] | None:
    """(user_id or None, login) for a GitHub noreply address, else None."""
    m = NOREPLY_RE.match(email.strip())
    if not m:
        return None
    return (int(m.group("id")) if m.group("id") else None, m.group("login"))


@dataclass
class IdentitySet:
    login: str
    user_id: int | None = None
    emails: dict[str, list[str]] = field(default_factory=dict)  # lowercased email -> evidence
    names: dict[str, set[str]] = field(default_factory=dict)  # email -> names seen (audit only)

    def add(self, email: str, evidence: str, name: str | None = None) -> None:
        if not email:
            return
        key = email.strip().lower()
        self.emails.setdefault(key, [])
        same_signal = [e for e in self.emails[key] if e.split(":")[0] == evidence.split(":")[0]]
        if evidence not in self.emails[key] and len(same_signal) < MAX_EVIDENCE_PER_SIGNAL:
            self.emails[key].append(evidence)
        if name:
            self.names.setdefault(key, set()).add(name)

    def evidence_for(self, email: str) -> list[str] | None:
        """Why this email belongs to the candidate, or None if it does not."""
        key = email.strip().lower().strip("<>")
        if key in self.emails:
            return self.emails[key]
        parsed = parse_noreply(key)
        if parsed:
            uid, login = parsed
            if login.lower() == self.login.lower() and (uid is None or self.user_id is None or uid == self.user_id):
                return ["noreply"]
        return None

    def matches(self, email: str) -> bool:
        return self.evidence_for(email) is not None

    def pairs(self) -> set[tuple[str, str]]:
        """(name, email) pairs known for the candidate (name may be empty)."""
        out: set[tuple[str, str]] = set()
        for email in self.emails:
            for name in self.names.get(email) or {""}:
                out.add((name, email))
        return out

    def canonical_emails(self, repo: Path) -> set[str]:
        """Identity emails mapped through ``repo``'s .mailmap, plus the
        originals. Blame/log emails are already canonical, so compare to this."""
        emails = set(self.emails)
        if self.user_id is not None:
            emails.add(f"{self.user_id}+{self.login}@users.noreply.github.com".lower())
        emails.add(f"{self.login}@users.noreply.github.com".lower())
        out = set(emails)
        for email in emails:
            try:
                mapped = run_git(["check-mailmap", f"<{email}>"], cwd=repo, timeout=30).decode().strip()
            except GitError:
                continue
            m = re.search(r"<([^>]+)>\s*$", mapped)
            if m:
                out.add(m.group(1).lower())
        return out


def build_identity_set(username: str, client=None, repos: list[dict] | None = None) -> IdentitySet:
    """Collect the candidate's git identities from GitHub. ``repos`` limits
    the commits-API lookups (signal 1) to the repositories being analysed."""
    from .github_client import GitHubClient

    own = client is None
    client = client or GitHubClient()
    try:
        profile = client.get_user(username)
        ident = IdentitySet(login=profile.get("login") or username, user_id=profile.get("id"))

        # 2. noreply addresses (deterministic from login + id)
        if ident.user_id is not None:
            ident.add(f"{ident.user_id}+{ident.login}@users.noreply.github.com", "noreply")
        ident.add(f"{ident.login}@users.noreply.github.com", "noreply")

        # 3. public profile email
        if profile.get("email"):
            ident.add(profile["email"], "profile_email", profile.get("name"))

        # 1. commits GitHub itself linked to this login (authoritative)
        for repo in repos or []:
            owner, name = repo["owner"]["login"], repo["name"]
            try:
                commits = client.list_commits(owner, name, author=ident.login)
            except GitHubNotFound:
                continue
            except GitHubError as exc:
                log.warning("commits lookup failed for %s/%s: %s", owner, name, exc)
                continue
            for c in commits:
                linked = (c.get("author") or {}).get("login")
                if not linked or linked.lower() != ident.login.lower():
                    continue  # GitHub did not link this commit to the candidate
                git_author = (c.get("commit") or {}).get("author") or {}
                ident.add(git_author.get("email", ""), f"github_commit_login:{name}@{c.get('sha', '')[:12]}",
                          git_author.get("name"))
        return ident
    finally:
        if own:
            client.close()
