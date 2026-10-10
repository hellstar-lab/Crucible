"""Phase 3 exceptions. Callers get a clear, typed reason, never a raw crash."""

from __future__ import annotations


class EvidenceError(Exception):
    """Base class for every Phase 3 error."""


class GitHubError(EvidenceError):
    """An unexpected GitHub API response."""


class GitHubAuthError(GitHubError):
    """GITHUB_TOKEN was rejected (401): expired, revoked or mistyped."""


class GitHubForbidden(GitHubError):
    """403 (not rate limiting) or 451: the repository exists but access is denied."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class GitHubNotFound(GitHubError):
    """The user or repository does not exist (or is no longer public)."""


class GitHubRateLimited(GitHubError):
    """The API rate limit is exhausted. ``reset_at`` is a Unix timestamp."""

    def __init__(self, message: str, reset_at: int | None, remaining: int | None) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.remaining = remaining


class GitHubNetworkError(GitHubError):
    """The API could not be reached."""


class GitError(EvidenceError):
    """A git command failed."""


class GitTimeout(GitError):
    """A git command exceeded its time limit and was killed."""
