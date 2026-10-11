"""Phase 4C end to end: resume -> claims (4A) -> evidence (4B) -> verdicts (4C), printed as the evidence table.

    python scripts/verdict_table.py RESUME --repo owner/name [--repo ...] --user LOGIN [--email ADDR ...] \
        [--runs 3] [--decomposition-tier nano] [--json out.json]

Live calls: 4A (Nebius), 3.6 analysis (GitHub API + clone), 4C (Nebius, Super, Ultra on escalation). 4B is
deterministic. 4A and 4B run ONCE; 4C runs --runs times on the same evidence, so run-to-run differences are 4C's own.
No scores, no ranking, no hire/reject signal: the table lists each claim's verdict, reason and citation.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import retrieval, verdicts  # noqa: E402
from backend.claims.pipeline import extract_claims  # noqa: E402
from backend.evidence import cache  # noqa: E402
from backend.evidence.target import Options, analyze_targets, identity_of  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("resume", type=Path)
    ap.add_argument("--repo", action="append", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--email", action="append", default=[])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--decomposition-tier", default="nano")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    extraction = extract_claims(analyze_document(a.resume), decomposition_tier=a.decomposition_tier, run_tag="verdict_table:4A")
    claims = extraction.claims or []
    print(f"4A: {len(claims)} claims, {len(extraction.excluded or [])} units excluded, {extraction.calls} calls, ${extraction.cost_usd}")

    report = analyze_targets(a.repo, user=a.user, emails=a.email or None, opts=Options())
    identity = identity_of(report)  # J4c: the identity 3.6 used (in-process only; reports stay redacted)
    views = []
    with tempfile.TemporaryDirectory(prefix="crucible-4c-") as tmp:
        for t in report.targets:
            if t.status not in ("resolved", "archived_ok") or t.repo_facts is None:
                print(f"3.6: {t.raw_input} -> {t.status}")
                continue
            checkout = Path(tmp) / t.repo_facts.full_name.replace("/", "__")
            import subprocess
            subprocess.run(["git", "clone", "--quiet", f"https://github.com/{t.repo_facts.full_name}.git", str(checkout)], check=False)
            views.append(retrieval.load_view(t, snapshot_root=cache.CACHE_DIR, checkout=checkout if checkout.exists() else None,
                                             identity=identity))
        sets = retrieval.retrieve_all(claims, views)
        print(f"4B: {sum(1 for s in sets if s.retrieval_performed)} searched, {sum(len(s.items) for s in sets)} items")

    runs = [verdicts.verdicts_for(claims, sets, run_tag=f"verdict_table:4C:{i}") for i in range(a.runs)]
    for i, r in enumerate(runs, 1):
        print(f"\n=== 4C run {i}: {r.calls} calls, ${r.cost_usd}, escalations {r.counters.get('escalations', 0)}, "
              f"greedy_disagreement_rate {r.greedy_disagreement_rate}, truncated {r.truncated}")
        for v in r.verdicts:
            cite = (f"[{v.cited_item_index}] {v.cited_item.rule} {v.cited_item.path}:{v.cited_item.start_line} "
                    f"{(v.cited_item.excerpt or v.cited_item.observed_value or '')[:60]!r}") if v.cited_item else "-"
            print(f"{v.claim_id:<10} {v.verdict:<13} {v.claim_text[:60]!r}")
            print(f"           row={v.matrix_row} tier={v.model_tier} escalated={v.escalated} content={v.content_supported} "
                  f"scope={v.scope}{'(' + v.scope_target + ')' if v.scope_target else ''} agreement={v.agreement}")
            for smp in v.samples or []:
                print(f"             sample[{smp['tier']}]: {smp['content_supported']} -> {smp['final_verdict']} "
                      f"cite={smp['cited_item_index']} p={smp['top_prob']} :: {(smp['content_reason'] or '')[:110]}")
            print(f"           reason: {v.reason}")
            if v.content_reason:
                print(f"           content_reason: {v.content_reason}")
            print(f"           citation: {cite}" + (f"   authorship: {v.authorship_note}" if v.authorship_note else ""))
    stable = Counter()
    for cid in {v.claim_id for r in runs for v in r.verdicts}:
        stable[len({v.verdict for r in runs for v in r.verdicts if v.claim_id == cid})] += 1
    print(f"\nacross {a.runs} runs: claims with 1 distinct verdict: {stable[1]}, with 2: {stable[2]}, with 3: {stable[3]}")
    if a.json:
        a.json.write_text(json.dumps({"extraction": extraction.model_dump(), "evidence": [s.model_dump() for s in sets],
                                      "runs": [r.model_dump() for r in runs]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
