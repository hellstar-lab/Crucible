"""Resolve an admin-supplied target string into a structured Target.

Pure function, no network, never raises. Admin input is untrusted: owner
and repo names must match ``^[A-Za-z0-9._-]+$`` before they can reach git or
the API. Nothing is guessed:

  * what a slashless token means depends on WHERE it was typed, so the caller
    says so: ``context="account"`` (or ``"auto"``) treats it as an account;
    ``context="repo"`` (behind --repo) treats it as a repository name owned by
    ``default_owner``, and is invalid without one. The string alone never decides;
  * ``/tree/<x>/<y>/...`` cannot say where the branch name ends, so every
    candidate (ref, path) split is returned with ``ref_ambiguous=True`` for
    the precheck to settle against the real branch list.
"""

from __future__ import annotations

import re
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}
_SSH_RE = re.compile(r"^git@github\.com:(?P<path>.+)$")


class Target(BaseModel):
    raw_input: str
    kind: Literal["repo", "account", "invalid"]
    owner: Optional[str] = None
    repo: Optional[str] = None
    ref: Optional[str] = None
    scoped_path: Optional[str] = None
    ref_ambiguous: bool = False
    ref_candidates: list[tuple[str, Optional[str]]] = []  # (ref, scoped_path) splits to try
    parse_warnings: list[str] = []
    invalid_reason: Optional[str] = None

    @property
    def full_name(self) -> Optional[str]:
        return f"{self.owner}/{self.repo}" if self.owner and self.repo else None

    @property
    def key(self) -> Optional[str]:
        """Case-insensitive identity for de-duplication (display keeps case)."""
        if self.kind == "account":
            return f"account:{(self.owner or '').lower()}"
        if self.kind == "repo":
            return f"repo:{self.full_name.lower()}:{self.ref or ''}:{self.scoped_path or ''}"  # type: ignore[union-attr]
        return None


def _invalid(raw: str, reason: str) -> Target:
    return Target(raw_input=raw, kind="invalid", invalid_reason=reason)


def _valid_name(name: str) -> bool:
    # A leading "-" could be read as a git option; GitHub names never start with "-" or ".".
    return bool(NAME_RE.match(name)) and name not in (".", "..") and not name.startswith(("-", "."))


def _clean_path(parts: list[str]) -> Optional[str]:
    parts = [p for p in parts if p]
    if not parts:
        return None
    if any(p in (".", "..") for p in parts):
        raise ValueError("path may not contain '.' or '..' segments")
    return "/".join(parts)


Context = Literal["auto", "account", "repo"]


def resolve_target(raw: str, context: Context = "auto", default_owner: str | None = None) -> Target:
    """Parse one target. Accepted forms are listed in the module tests; an
    unparseable input returns kind="invalid" with a reason.

    context="auto" / "account": a slashless token is an account.
    context="repo": a slashless token is a repository NAME owned by
    ``default_owner`` (invalid when there is none); an account is never
    accepted. context="account" rejects owner/repo forms."""
    text = (raw or "").strip()
    warnings: list[str] = []
    if not text:
        return _invalid(raw, "empty input")
    if any(c.isspace() for c in text):
        return _invalid(raw, "input contains whitespace")

    # --- SSH form ---------------------------------------------------------
    m = _SSH_RE.match(text)
    if m:
        path = m.group("path")
    elif "://" in text or text.lower().startswith(("github.com/", "www.github.com/")):
        if "://" not in text:
            text_url = "https://" + text
        else:
            text_url = text
        try:
            url = urlsplit(text_url)
        except ValueError:
            return _invalid(raw, "malformed URL")
        if url.scheme not in ("http", "https"):
            return _invalid(raw, f"unsupported URL scheme {url.scheme!r}")
        if url.hostname not in _GITHUB_HOSTS:
            return _invalid(raw, f"not a github.com URL (host {url.hostname!r})")
        if url.scheme == "http":
            warnings.append("http URL upgraded to https")
        if url.query:
            warnings.append(f"query string ignored: ?{url.query}")
        if url.fragment:
            warnings.append(f"fragment ignored: #{url.fragment}")
        if url.username or url.password or url.port:
            return _invalid(raw, "URL with credentials or a port is not accepted")
        path = url.path
    else:
        if text.startswith("/"):
            return _invalid(raw, "leading slash")
        path = text

    segments = [s for s in path.split("/") if s != ""]
    if not segments:
        return _invalid(raw, "no owner or repository in input")

    # --- a single token: meaning comes from the caller's context ------------
    if len(segments) == 1:
        name = segments[0]
        if context == "repo":
            if not default_owner:
                return _invalid(raw, f"bare repository name {name!r} with no owner and no username; "
                                     "give owner/repo or a username")
            if not _valid_name(default_owner):
                return _invalid(raw, f"invalid owner name {default_owner!r}")
            repo_name = name[:-4] if name.endswith(".git") else name
            if not _valid_name(repo_name):
                return _invalid(raw, f"invalid repository name {repo_name!r}")
            return Target(raw_input=raw, kind="repo", owner=default_owner, repo=repo_name,
                          parse_warnings=warnings + [f"bare name qualified with owner {default_owner!r}"])
        if name.endswith(".git"):
            return _invalid(raw, "a single name ending in .git is ambiguous; give owner/repo")
        if not _valid_name(name):
            return _invalid(raw, f"invalid account name {name!r}")
        return Target(raw_input=raw, kind="account", owner=name, parse_warnings=warnings)
    if context == "account":
        return _invalid(raw, "expected an account, got an owner/repo reference")

    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _valid_name(owner):
        return _invalid(raw, f"invalid owner name {owner!r}")
    if not _valid_name(repo):
        return _invalid(raw, f"invalid repository name {repo!r}")

    rest = segments[2:]
    ref = scoped = None
    candidates: list[tuple[str, Optional[str]]] = []
    if rest:
        kind, tail = rest[0], rest[1:]
        if kind in ("tree", "blob") and tail:
            try:
                # Where the branch name ends is unknowable from the URL alone: every
                # split (ref = tail[:i], path = tail[i:]) is a candidate. The first is
                # only provisional until the precheck checks the real branch list.
                candidates = [("/".join(tail[:i]), _clean_path(tail[i:])) for i in range(1, len(tail))]
                candidates.append(("/".join(tail), None))
            except ValueError as exc:
                return _invalid(raw, str(exc))
            ref, scoped = candidates[0]
            if any(not REF_RE.match(r) for r, _p in candidates):
                return _invalid(raw, f"invalid ref in {'/'.join(tail)!r}")
            if len(candidates) == 1:
                candidates = []  # tree/<ref> with nothing after it: unambiguous
        elif kind in ("pull", "pulls", "issues", "commit", "commits", "actions", "releases", "wiki", "compare",
                      "discussions", "projects", "security", "settings", "tags", "branches", "network"):
            warnings.append(f"'/{'/'.join(rest)}' ignored; analysing the repository")
        else:
            warnings.append(f"unrecognised path '/{'/'.join(rest)}' ignored; analysing the repository")
    ambiguous = bool(candidates)
    return Target(raw_input=raw, kind="repo", owner=owner, repo=repo, ref=ref, scoped_path=scoped,
                  ref_ambiguous=ambiguous, ref_candidates=candidates,
                  parse_warnings=warnings)


def settle_ref(target: Target, branches: list[str], default_branch: str) -> Target:
    """Pick the (ref, path) split that names a real branch. Never guesses:
    no match, or more than one, falls back to the default branch with a warning."""
    if not target.ref_ambiguous:
        return target
    known = set(branches)
    matches = [(r, p) for r, p in target.ref_candidates if r in known]
    t = target.model_copy(deep=True)
    if len(matches) == 1:
        t.ref, t.scoped_path = matches[0]
        t.parse_warnings.append(f"ref resolved against branch list: {t.ref!r}, path {t.scoped_path!r}")
    else:
        t.ref, t.scoped_path = default_branch, None
        why = "no candidate matches a branch" if not matches else f"{len(matches)} candidates match branches"
        t.parse_warnings.append(f"ambiguous ref/path ({why}); fell back to default branch {default_branch!r}, "
                                "whole repository")
    t.ref_ambiguous = False
    return t
