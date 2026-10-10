"""Minimal GitHub REST client for Phase 3 (public data only).

Authenticates with GITHUB_TOKEN from os.environ when present (5,000 req/h);
works unauthenticated at 60 req/h. Rate limiting raises GitHubRateLimited
with the reset time instead of sleeping, so callers decide how to back off.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterator

import httpx

from . import config
from .errors import GitHubAuthError, GitHubError, GitHubForbidden, GitHubNetworkError, GitHubNotFound, GitHubRateLimited

log = logging.getLogger(__name__)

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubClient:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "crucible-evidence",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.authenticated = bool(token)
        self.calls_made = 0
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: int | None = None
        self._http = httpx.Client(base_url=config.GITHUB_API, headers=headers,
                                  timeout=config.HTTP_TIMEOUT_S, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            resp = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise GitHubNetworkError(f"GitHub unreachable: {type(exc).__name__}") from exc
        self.calls_made += 1
        if "x-ratelimit-remaining" in resp.headers:
            self.rate_limit_remaining = int(resp.headers["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in resp.headers:
            self.rate_limit_reset = int(resp.headers["x-ratelimit-reset"])
        log.info("GitHub %s %s -> %d; X-RateLimit-Remaining=%s", resp.request.method, resp.request.url.path,
                 resp.status_code, self.rate_limit_remaining)

        if resp.status_code == 401:
            raise GitHubAuthError("GITHUB_TOKEN was rejected by GitHub (401); it is expired, revoked or "
                                  "mistyped. Fix it, or unset it to run unauthenticated (60 req/h).")
        if resp.status_code == 404:
            raise GitHubNotFound(f"not found: {resp.request.url.path}")
        if resp.status_code in (403, 429) and (
            self.rate_limit_remaining == 0 or "retry-after" in resp.headers
        ):
            reset = self.rate_limit_reset
            if "retry-after" in resp.headers and reset is None:
                reset = int(time.time()) + int(resp.headers["retry-after"])
            raise GitHubRateLimited("GitHub API rate limit exhausted", reset_at=reset,
                                    remaining=self.rate_limit_remaining)
        if resp.status_code in (403, 451):
            raise GitHubForbidden(f"access denied ({resp.status_code}) for {resp.request.url.path}: "
                                  f"{_message(resp)}", resp.status_code)
        if resp.status_code >= 400 and resp.status_code != 409:
            raise GitHubError(f"GitHub API {resp.status_code} for {resp.request.url.path}: {_message(resp)}")
        return resp

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(url, params).json()

    def _paginate(self, url: str, params: dict[str, Any] | None = None, max_pages: int | None = None) -> Iterator[Any]:
        params = {"per_page": 100, **(params or {})}
        pages = 0
        next_url: str | None = url
        while next_url and (max_pages is None or pages < max_pages):
            resp = self._request(next_url, params if pages == 0 else None)
            pages += 1
            if resp.status_code == 409:  # "Git Repository is empty"
                return
            yield from resp.json()
            m = _NEXT_LINK.search(resp.headers.get("link", ""))
            next_url = m.group(1) if m else None

    # ------------------------------------------------------------------
    def get_user(self, username: str) -> dict:
        return self._get(f"/users/{username}")

    def list_repos(self, username: str) -> list[dict]:
        """Public repositories owned by the user (not ones they merely contribute to)."""
        return list(self._paginate(f"/users/{username}/repos", {"type": "owner", "sort": "pushed"}))

    def get_repo(self, owner: str, repo: str) -> dict:
        return self._get(f"/repos/{owner}/{repo}")

    def list_commits(self, owner: str, repo: str, author: str | None = None,
                     max_pages: int = config.COMMITS_API_MAX_PAGES, sha: str | None = None) -> list[dict]:
        """Commits on the default branch (or ``sha``). ``author`` filters to
        commits GitHub has linked to that login (or matching that email)."""
        params = {k: v for k, v in (("author", author), ("sha", sha)) if v} or None
        return list(self._paginate(f"/repos/{owner}/{repo}/commits", params, max_pages=max_pages))

    def get_repo_meta(self, owner: str, repo: str) -> tuple[dict, str | None]:
        """Repository metadata, following 301 Moved Permanently (renames and
        transfers). Returns (metadata, redirected_from or None)."""
        resp = self._request(f"/repos/{owner}/{repo}")
        redirected_from = None
        hops = 0
        while resp.status_code in (301, 302, 307, 308) and hops < 3:
            location = resp.headers.get("location") or (resp.json() or {}).get("url")
            if not location:
                raise GitHubError(f"redirect without a location for {owner}/{repo}")
            redirected_from = f"{owner}/{repo}"
            resp = self._request(location)
            hops += 1
        meta = resp.json()
        if redirected_from and meta.get("full_name", "").lower() == redirected_from.lower():
            redirected_from = None  # case-only difference, not a rename
        return meta, redirected_from

    def list_branches(self, owner: str, repo: str, max_pages: int = 10) -> list[str]:
        return [b["name"] for b in self._paginate(f"/repos/{owner}/{repo}/branches", max_pages=max_pages)]

    def compare(self, owner: str, repo: str, base: str, head: str, max_pages: int = 10) -> dict:
        """Paginated compare. ``files`` appears only on the first page; commits
        are collected across pages. Adds ``commits_complete`` and
        ``files_truncated`` so callers never mistake a capped list for a full one."""
        url = f"/repos/{owner}/{repo}/compare/{base}...{head}"
        first = self._request(url, {"per_page": 100, "page": 1}).json()
        commits = list(first.get("commits") or [])
        total = first.get("total_commits") or 0
        page = 1
        while len(commits) < total and page < max_pages:
            page += 1
            more = self._request(url, {"per_page": 100, "page": page}).json().get("commits") or []
            if not more:
                break
            commits.extend(more)
        first["commits"] = commits
        first["commits_complete"] = len(commits) >= total
        first["files_truncated"] = len(first.get("files") or []) >= config.COMPARE_FILES_CAP
        return first

    def get_commit(self, owner: str, repo: str, sha: str) -> dict:
        return self._get(f"/repos/{owner}/{repo}/commits/{sha}")

    def get_tree(self, owner: str, repo: str, ref: str) -> dict:
        return self._get(f"/repos/{owner}/{repo}/git/trees/{ref}", {"recursive": "1"})

    def get_raw(self, owner: str, repo: str, path: str, ref: str | None = None) -> str:
        """A file's raw text via the contents API (used only in degraded mode)."""
        resp = self._http.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref} if ref else None,
                              headers={"Accept": "application/vnd.github.raw+json"})
        self.calls_made += 1
        if "x-ratelimit-remaining" in resp.headers:
            self.rate_limit_remaining = int(resp.headers["x-ratelimit-remaining"])
        if resp.status_code == 404:
            raise GitHubNotFound(f"not found: {path}")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub API {resp.status_code} for contents/{path}")
        return resp.text


def _message(resp: httpx.Response) -> str:
    try:
        return str((resp.json() or {}).get("message", ""))[:200]
    except ValueError:
        return ""
