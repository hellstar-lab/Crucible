"""Analyse a GitHub account, or specific repositories (thin wrapper over analyze.py).

    python scripts/analyze_repos.py <username> [--max-repos N]          # Mode A: the account's repositories
    python scripts/analyze_repos.py <username> --repo <name>            # Mode B: only <username>/<name>
    python scripts/analyze_repos.py --repo owner/repo                   # Mode B: any resolver form, e.g.
    python scripts/analyze_repos.py --repo https://github.com/o/r/tree/<branch>[/<path>]

Resolution table (implemented exactly):
    USER                      -> enumerate USER's repositories
    USER --repo NAME          -> analyse USER/NAME only, identity = USER
    USER --repo OWNER/NAME    -> analyse OWNER/NAME only, identity = USER (OSS contribution case)
    --repo OWNER/NAME | URL   -> analyse it, authorship = None (no identity)
    --repo NAME               -> ERROR: bare repository name with no owner and no username
Identity precedence: --email (repeatable) > the positional username > none.
The username is the identity source, not a filter on whose repositories --repo may name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze  # noqa: E402
from backend.evidence import config  # noqa: E402


def owner_notes(username: str | None, targets: list[str]) -> list[str]:
    """Say explicitly when a requested repository is owned by someone other than
    the username: identity still comes from the username (Phase 3.6 rule)."""
    from backend.evidence.resolve import resolve_target

    notes = []
    for raw in targets:
        t = resolve_target(raw, context="repo", default_owner=username)
        if username and t.kind == "repo" and t.owner and t.owner.lower() != username.lower():
            notes.append(f"note: {t.full_name} is owned by {t.owner!r}, not {username!r}; identity comes from "
                         f"{username!r} and only the listed repositories are analysed")
    return notes


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", nargs="?", help="GitHub account (identity source; enumerated if no --repo)")
    ap.add_argument("--repo", action="append", default=[], help="repeatable; bare name, owner/repo or URL")
    ap.add_argument("--email", action="append", default=[], help="repeatable; identity source, takes precedence")
    ap.add_argument("--max-repos", type=int, default=None, help="account mode cap (default MAX_REPOS)")
    ap.add_argument("--max-targets", type=int, default=None, help="--repo mode cap (default MAX_TARGETS_PER_RUN)")
    ap.add_argument("--json")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)
    if not args.username and not args.repo:
        ap.error("give a username, --repo, or both")
    from backend.evidence.resolve import resolve_target

    for raw in args.repo:  # the one resolution-table error that must stop the run
        t = resolve_target(raw, context="repo", default_owner=args.username)
        if t.kind == "invalid" and (t.invalid_reason or "").startswith("bare repository name"):
            ap.error(f"--repo {raw!r}: {t.invalid_reason}")
    targets = list(args.repo)  # resolved by the pipeline with the same context
    # "--opt=value" so a value beginning with "-" can never be re-read as an option
    forwarded = [f"--user={args.username}"] if args.username else []
    forwarded += [f"--repo={t}" for t in targets]
    forwarded += [f"--email={e}" for e in args.email]
    if targets:
        cap = args.max_targets if args.max_targets is not None else args.max_repos
        if cap is not None:
            forwarded += ["--max-targets", str(cap)]
    else:
        forwarded += ["--max-targets", str(args.max_repos if args.max_repos is not None else config.MAX_REPOS)]
    if args.json:
        forwarded += ["--json", args.json]
    if args.no_cache:
        forwarded.append("--no-cache")
    for note in owner_notes(args.username, targets):
        print(note)
    return analyze.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
