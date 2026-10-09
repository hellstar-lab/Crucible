"""Phase 3 CLI: authorship attribution for a GitHub user's public repositories.
Deterministic; makes no model calls.

    python scripts/analyze_repos.py <username> [--max-repos N]
"""

from dotenv import load_dotenv

load_dotenv()  # entry point only (GITHUB_TOKEN); library code reads os.environ

import argparse  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.evidence import analyze_github_detailed, config  # noqa: E402
from backend.evidence.errors import EvidenceError, GitHubAuthError, GitHubNotFound, GitHubRateLimited  # noqa: E402

COLS = [("repo", 28), ("language", 11), ("src files", 9), ("total ln", 9), ("code ln", 8), ("authored", 9),
        ("auth %", 7), ("fork", 5), ("span d", 7), ("tests", 6), ("1-commit", 8), ("clustered", 9)]


def _fmt(v, width: int) -> str:
    """Numbers right-aligned, text left-aligned, truncated to the column."""
    right = isinstance(v, (int, float)) and not isinstance(v, bool)
    s = "-" if v is None else ("yes" if v else "no") if isinstance(v, bool) else str(v)
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.rjust(width) if right or v is None else s.ljust(width)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--max-repos", type=int, default=config.MAX_REPOS)
    args = ap.parse_args(argv)
    try:
        result = analyze_github_detailed(args.username, max_repos=args.max_repos)
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except GitHubNotFound:
        print(f"error: GitHub user '{args.username}' not found", file=sys.stderr)
        return 1
    except GitHubRateLimited as exc:
        when = datetime.fromtimestamp(exc.reset_at, timezone.utc).isoformat() if exc.reset_at else "unknown"
        print(f"error: GitHub rate limit exhausted; resets at {when}", file=sys.stderr)
        return 1
    except EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"user: {result.username}   authenticated: {'yes' if result.authenticated else 'no (60 req/h)'}")
    print(" ".join(_fmt(name, w) for name, w in COLS))
    print("-" * (sum(w for _, w in COLS) + len(COLS) - 1))
    skipped = []
    for r in result.repos:
        if r.skip_reason:
            skipped.append(r)
            continue
        pct = round(100 * r.authored_ratio, 1) if r.authored_ratio is not None else None
        row = [r.name, r.language or "-", r.source_files, r.total_lines, r.code_lines, r.authored_lines, pct,
               r.is_fork, r.commit_span_days, r.tests_present, r.single_commit_import, r.committer_dates_clustered]
        print(" ".join(_fmt(v, w) for v, (_, w) in zip(row, COLS)))
        if r.likely_identity_gap and r.unmatched_authors:
            top = r.unmatched_authors[0]
            print(f"    NOTE: 0% authored, but one unmatched author ({top.email_redacted}) wrote "
                  f"{top.authored_lines} lines. The candidate may have committed under an unlinked git "
                  "identity. Verify before relying on this number.")
        for note in r.notes:
            print(f"    note: {note}")
    if skipped:
        print("\nskipped:")
        for r in skipped:
            print(f"  {r.name:<28} {r.skip_reason}")

    print("\nidentities accepted (email -> evidence):")
    for email, evidence in sorted(result.identity.emails.items()) if result.identity else []:
        print(f"  {email:<55} {', '.join(evidence)}")
    print("  + any <id>+<login>@users.noreply.github.com / <login>@users.noreply.github.com address (noreply rule)")
    remaining = result.rate_limit_remaining if result.rate_limit_remaining is not None else "unknown"
    print(f"\nwall time: {result.elapsed_s:.1f}s   GitHub API calls: {result.api_calls}   rate limit remaining: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
