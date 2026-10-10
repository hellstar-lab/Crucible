"""Phase 3.5 Step 5: re-run the pipeline and diff it against the pre-3.5
baseline. Writes the post measurement next to the baseline. Deterministic;
no model calls.

    python scripts/compare_baseline.py hellstar-lab [--evidence REPO ...]
"""

from dotenv import load_dotenv

load_dotenv()  # entry point only (GITHUB_TOKEN)

import argparse  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.evidence import (_gitattributes, _list_files, _measure, _raw_line_counts, _text_paths,  # noqa: E402
                              analyze_github_detailed)
from backend.evidence.clone import cloned_repo  # noqa: E402
from backend.evidence.github_client import GitHubClient  # noqa: E402

BASELINE = ROOT / "backend" / "evidence" / "baseline_pre35.json"
POST = ROOT / "backend" / "evidence" / "measurement_post35.json"


def _pct(r):
    return None if r is None else round(100 * r, 1)


def _d(new, old):
    return None if new is None or old is None else new - old


def evidence(clone_url: str, n: int = 8) -> dict:
    """Top counted files (by source lines) and top excluded files, for one repo."""
    with cloned_repo(clone_url) as repo, tempfile.TemporaryDirectory() as tmp:
        counted, excluded, _ = _measure(repo, _list_files(repo), _gitattributes(repo), _text_paths(repo),
                                        _raw_line_counts(repo), Path(tmp))
    top_counted = sorted(counted, key=lambda c: (-c.source_count, c.path))[:n]
    return {
        "top_counted": [{"path": c.path, "language": c.language, "source": c.source_count, "code": c.code,
                         "string": c.string, "documentation": c.documentation} for c in top_counted],
        "top_excluded": [{"path": p, "lines": ln, "reason": r}
                         for p, ln, r in sorted(excluded, key=lambda e: (-e[1], e[0]))[:n]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--evidence", nargs="*", default=[])
    args = ap.parse_args()

    base = {r["name"]: r for r in json.loads(BASELINE.read_text())["repos"]}
    result = analyze_github_detailed(args.username)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    post = {"measurement": "post-phase-3.5", "username": args.username, "code_commit": commit,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counting": "pygount source lines (code + string) in countable files; authored = authored source lines",
            "repos": [r.model_dump(include={"name", "skip_reason", "total_lines", "code_lines", "source_lines",
                                            "documentation_lines", "string_lines", "empty_lines", "authored_lines",
                                            "authored_ratio", "source_files", "excluded_files_by_reason",
                                            "excluded_lines_by_reason", "top_excluded_files"})
                      for r in result.repos]}
    POST.write_text(json.dumps(post, indent=2) + "\n")

    hdr = (f"{'repo':<24}{'files o→n':>12}{'total o':>9}{'total n':>9}{'Δtotal':>9}"
           f"{'auth o':>8}{'auth n':>8}{'Δauth':>8}{'ratio o':>9}{'ratio n':>9}{'Δpts':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in result.repos:
        if r.skip_reason:
            continue
        b = base.get(r.name, {})
        old_ratio, new_ratio = _pct(b.get("authored_ratio")), _pct(r.authored_ratio)
        print(f"{r.name:<24}{str(b.get('source_files')) + '→' + str(r.source_files):>12}"
              f"{b.get('total_lines', '-')!s:>9}{r.total_lines!s:>9}{_d(r.total_lines, b.get('total_lines'))!s:>9}"
              f"{b.get('authored_lines', '-')!s:>8}{r.authored_lines!s:>8}"
              f"{_d(r.authored_lines, b.get('authored_lines'))!s:>8}"
              f"{old_ratio!s:>9}{new_ratio!s:>9}{_d(new_ratio, old_ratio) if new_ratio is not None else '-'!s:>7}")
    print("\nnew basis: source lines (code + string) per repo:")
    for r in result.repos:
        if not r.skip_reason:
            excl = {k: v for k, v in r.excluded_lines_by_reason.items() if v}
            print(f"  {r.name:<24} source={r.source_lines} (code {r.code_lines} + string {r.string_lines}); "
                  f"docs/comments {r.documentation_lines}; excluded lines {excl}")

    with GitHubClient() as gh:
        metas = {m["name"]: m for m in gh.list_repos(args.username)}
    for name in args.evidence:
        ev = evidence(metas[name]["clone_url"])
        print(f"\n=== evidence: {name}")
        print("  top counted files (source lines):")
        for c in ev["top_counted"]:
            print(f"    {c['source']:>6}  {c['language']:<12} code {c['code']:>5} string {c['string']:>5}  {c['path']}")
        print("  top excluded files:")
        for e in ev["top_excluded"]:
            print(f"    {e['lines']:>6}  {e['reason']:<18} {e['path']}")
    print(f"\nwrote {POST.relative_to(ROOT)}; wall {result.elapsed_s:.1f}s; API calls {result.api_calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
