"""Phase 3.6 CLI: analyse explicit repositories, an account, or both.
Deterministic; makes no model calls.

    python scripts/analyze.py --user LOGIN
    python scripts/analyze.py --repo owner/repo --repo https://github.com/o/r/tree/main/src
    python scripts/analyze.py --user LOGIN --repo owner/repo --json out.json
    python scripts/analyze.py --repos-file targets.txt --email me@example.com
"""

from dotenv import load_dotenv

load_dotenv()  # entry point only (GITHUB_TOKEN); library code reads os.environ

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.evidence import config  # noqa: E402
from backend.evidence.errors import GitHubAuthError  # noqa: E402
from backend.evidence.target import Options, analyze_targets  # noqa: E402


def _targets_file(path: str) -> list[str]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _pct(v):
    return "-" if v is None else f"{100 * v:.1f}%"


def render(report) -> None:
    spec = report.input_spec
    print(f"run: {spec.run_mode}")
    print(f"identity source: {spec.identity_source or 'none'}")
    for i in spec.inputs:
        extra = f"  [{'; '.join(i['warnings'])}]" if i["warnings"] else ""
        print(f"  input {i['raw']!r} -> {i['kind']} {i['resolved_to'] or ''}"
              f"{' @' + i['ref'] if i['ref'] else ''}{' path=' + i['scoped_path'] if i['scoped_path'] else ''}"
              f"{' INVALID: ' + i['invalid_reason'] if i['invalid_reason'] else ''}{extra}")
    for d in spec.duplicates:
        print(f"  duplicate: {d['raw']!r} = {d['duplicate_of']!r}")
    for r in report.targets:
        f, a, q = r.repo_facts, r.authorship, r.question_surface
        name = f.full_name if f else (r.target.get("owner") or "?") + "/" + (r.target.get("repo") or "?")
        print(f"\n== {name}  status={r.status}  mode={r.mode or '-'}{'  (cache hit)' if r.cache_hit else ''}")
        if r.message:
            print(f"   {r.message}")
        # branch fields, not prose: what was asked for vs what was actually analysed
        default = " (default branch)" if f and r.ref_analyzed and r.ref_analyzed == f.default_branch else ""
        print(f"   ref requested: {r.ref_requested or '(default branch)'} | ref analysed: "
              f"{(r.ref_analyzed + default) if r.ref_analyzed else '(nothing analysed)'}"
              + (f" | path {f.scoped_path or '(whole repository)'}" if f else ""))
        for w in r.target.get("parse_warnings") or []:
            print(f"   warning: {w}")
        if f:
            h = f.history
            print(f"   facts: {f.source_files} source files, {f.total_source_lines} source lines; languages "
                  f"{dict(list((f.languages or {}).items())[:4])}")
            if h:
                print(f"   history: {h.commit_count} commits, {h.distinct_author_count} author(s), {h.span_days} days"
                      f"{', single-commit import' if h.single_commit_import else ''}"
                      f"{', one session' if h.committed_in_one_session else ''}")
            if f.is_fork:
                print(f"   fork of {f.parent_full_name}: ahead {f.fork_ahead_by}, behind {f.fork_behind_by}"
                      f"{' (files truncated)' if f.files_truncated else ''}")
            excl = {k: v for k, v in f.excluded_files_by_reason.items() if v}
            print(f"   excluded files: {excl or 'none'}; dependencies {len(f.dependencies)} from "
                  f"{len(f.manifests)} manifest(s); README claims {len(f.readme_claims)}")
        if a:
            line = f"   authorship ({a.mode}): {_pct(a.authored_ratio)}"
            if a.mode == "blame":
                line += f" ({a.authored_source_lines}/{a.total_source_lines} source lines)"
            print(line + f"; confidence {a.authorship_confidence}")
            if not a.authored_ratio_meaningful:
                print(f"     NOT MEANINGFUL: {a.authorship_confidence_reason}")
            if a.authored_ratio_whole_repo is not None or a.fork_delta_reason:
                print(f"     whole repo {_pct(a.authored_ratio_whole_repo)} (misleading on a fork); fork delta "
                      f"{_pct(a.authored_ratio_fork_delta)}{' - ' + a.fork_delta_reason if a.fork_delta_reason else ''}")
            if a.likely_identity_gap and a.unmatched_authors:
                top = a.unmatched_authors[0]
                print(f"     NOTE: one unmatched author ({top.email_redacted}) wrote {top.authored_lines} lines; the "
                      "candidate may have committed under an unlinked git identity.")
        elif r.identity_unresolved_reason and r.status in ("resolved", "archived_ok", "too_large"):
            print(f"   authorship: not computed - {r.identity_unresolved_reason}")
        if q:
            eligible = sum(1 for b in q.bug_targets if b.eligible)
            verdicts = {}
            for c in q.claim_vs_code:
                verdicts[c.verdict] = verdicts.get(c.verdict, 0) + 1
            print(f"   question surface: {len(q.anchors)} anchors, {len(q.authored_functions)} authored functions "
                  f"({len(q.boundary_disagreements)} boundary disagreements), {eligible} eligible bug targets; "
                  f"claims {verdicts or '{}'}; gaps {q.coverage_gaps or 'none'}")
        for n in r.notes[:5]:
            print(f"   note: {n}")
    print(f"\nAPI calls {report.api_calls}; rate limit remaining {report.rate_limit_remaining}; "
          f"{report.elapsed_s}s; ruleset {report.exclusion_ruleset_version}"
          f"{'; STOPPED: ' + report.stopped_reason if report.stopped_reason else ''}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--repos-file")
    ap.add_argument("--email", action="append", default=[])
    ap.add_argument("--path")
    ap.add_argument("--ref")
    ap.add_argument("--max-repo-mb", type=int, default=config.MAX_REPO_MB)
    ap.add_argument("--max-targets", type=int, default=config.MAX_TARGETS_PER_RUN)
    ap.add_argument("--timeout", type=int, default=config.ANALYSIS_TIMEOUT_S)
    ap.add_argument("--json")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)
    raws = list(args.repo) + (_targets_file(args.repos_file) if args.repos_file else [])
    if not raws and not args.user:
        ap.error("give --user and/or --repo / --repos-file")
    opts = Options(max_targets=args.max_targets, max_repo_mb=args.max_repo_mb, analysis_timeout_s=args.timeout,
                   use_cache=not args.no_cache, path_override=args.path, ref_override=args.ref)
    try:
        report = analyze_targets(raws, user=args.user, emails=args.email, opts=opts)
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    render(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report.model_dump(mode="json"), indent=2))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
