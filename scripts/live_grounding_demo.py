"""Phase 4D live end-to-end on the clean_small fixture repo: the Bob's-ledger ownership claim through 4B -> 4C -> 4D.

    python scripts/live_grounding_demo.py [--runs 3] [--json out.json] [--report-dir reports]

--report-dir writes each run's ClaimEvidenceReport as clean_small_run<N>.json (render with scripts/render_report.py).

clean_small is built locally (tests/fixtures/make_git_fixtures.py) and analysed by the real 3.6 pipeline with a mocked
GitHub API, so the only live calls are 4C's content judgements (Nebius). The candidate wrote the cache and the API; Bob
wrote src/billing/ledger.py. The claim "Designed the ledger reconciliation module" is an ownership claim on Bob's code,
so this exercises the contradiction path live: scope rule, escalation to Ultra, and grounding.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "tests" / "fixtures"):
    sys.path.insert(0, str(p))

import make_git_fixtures as M  # noqa: E402
import test_target as T  # noqa: E402
from backend import grounding, retrieval, verdicts  # noqa: E402
from backend.claims.routing import route, scope_of  # noqa: E402
from backend.evidence import cache  # noqa: E402
from backend.evidence.identity import IdentitySet  # noqa: E402
from backend.grounding.report import build_report, render  # noqa: E402

CLAIMS = ["Designed the ledger reconciliation module", "Built the cache client", "Built a caching layer with Redis",
          "Led a team of 6 engineers"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--report-dir", type=Path)
    a = ap.parse_args()
    logging.disable(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="crucible-4d-") as tmp:
        tmp = Path(tmp)
        cache.CACHE_DIR = tmp / "cache"
        repo = tmp / "clean_small"
        M.clean_small(repo)
        report = T.run([f"{M.LOGIN}/clean_small"], {f"/repos/{M.LOGIN}/clean_small": T.meta(M.LOGIN, "clean_small")},
                       {f"{M.LOGIN}/clean_small": repo}, use_cache=True).targets[0]
        ident = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)
        ident.add(M.CAND[1], "profile_email")
        view = retrieval.load_view(report, snapshot_root=cache.CACHE_DIR, checkout=repo, identity=ident)
        claims = []
        for i, text in enumerate(CLAIMS):
            rt = route(text)
            kind = "ownership" if text.startswith(("Designed", "Built the")) else rt["kind"]
            scope, target = scope_of(text, kind)
            claims.append({"claim_id": f"c{i}", "text": text, "kind": kind, "checkable_from_code": "no" if kind == "role_or_team" else "yes",
                           "scope": scope, "scope_target": target})
        sets = retrieval.retrieve_all(claims, [view])
        dump = []
        for run in range(1, a.runs + 1):
            out = verdicts.verdicts_for(claims, sets, run_tag=f"live_grounding:{run}")
            grounded, failures = grounding.ground_all(out.verdicts, sets, [view])
            rep = build_report(verdicts=grounded, evidence_sets=sets, target_reports=[report], grounding_failures=failures, run=out,
                               run_label=f"Live run {run} of {a.runs} on the clean_small fixture repository; no resume document "
                                         f"(the claims are fixed strings).")
            if a.report_dir:
                a.report_dir.mkdir(parents=True, exist_ok=True)
                (a.report_dir / f"clean_small_run{run}.json").write_text(rep.model_dump_json(indent=1))
            print(f"\n######## run {run}: requests={out.calls} cost=${out.cost_usd} escalations={out.counters.get('escalations', 0)} "
                  f"greedy_disagreement_rate={out.greedy_disagreement_rate} ensemble_disagreements={out.counters.get('ensemble_disagreements', 0)} "
                  f"escalations_on_disagreement={out.counters.get('escalations_on_disagreement', 0)} "
                  f"resolved_by_ultra={out.counters.get('disagreements_resolved_by_ultra', 0)}")
            for v in grounded:
                if v.samples:
                    print(f"  {v.claim_id} samples: " + "; ".join(f"[{s['tier']}] {s['content_supported']}->{s['final_verdict']} cite={s['cited_item_index']}"
                                                         for s in v.samples) + f"  scope={v.scope}({v.scope_target}) "
                          f"scope_match={v.scope_match_rule}/{v.scope_match_score} escalated={v.escalated}")
                    if v.content_reason:
                        print(f"     content_reason: {v.content_reason}")
            print(render(rep))
            dump.append({"verdicts": [v.model_dump() for v in grounded], "report": rep.model_dump(), "counters": out.counters,
                         "calls": out.calls, "cost_usd": out.cost_usd})
        if a.json:
            a.json.write_text(json.dumps(dump, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
