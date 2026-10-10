"""Crucible end to end: resume -> 2C -> Shield gate -> 4A -> 3.6 -> 4B -> 4C -> 4D -> report page. One command.

    python scripts/crucible.py --resume FILE --user GITHUB_USER [--repo owner/name ...] [--email ADDR ...]
                               [--reference OPAQUE_REF] --out reports/NAME.html

Claims come only from the resume (4A); nothing is hand-written. Repositories come from --repo, or, when none is given,
from GitHub repository links found in the resume. Writes NAME.html (self-contained) and NAME.json (the
ClaimEvidenceReport) next to it, and prints the claim table and per-stage accounting.

PROMPT AUDIT: every model request in this process is scanned, before it is sent, for each hidden fragment the Shield
found and for "IGNORE ALL PREVIOUS INSTRUCTIONS". logs/model_calls.jsonl stores prompt HASHES, not text, so grepping it
cannot catch a leak; this in-process check can. A hit is reported and the command exits 3.
No scores, no ranking, no hire/reject signal.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import grounding, llm, retrieval, verdicts  # noqa: E402
from backend.claims.pipeline import extract_claims  # noqa: E402
from backend.evidence import cache  # noqa: E402
from backend.evidence.github_client import GitHubClient  # noqa: E402
from backend.evidence.identity import IdentitySet, build_identity_set  # noqa: E402
from backend.evidence.target import Options, analyze_targets  # noqa: E402
from backend.grounding.html import render_html  # noqa: E402
from backend.grounding.report import build_report  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402

CANARY = "IGNORE ALL PREVIOUS INSTRUCTIONS"
_GH_REPO = re.compile(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")


class PromptAudit:
    """Wraps llm.call: per-task request / cost tally, and a scan of every outgoing prompt for hidden text."""

    def __init__(self, needles: list[str]):
        self.needles = [n for n in {*(s.strip() for s in needles), CANARY} if len(n) >= 12]
        self.requests, self.cost, self.hits = Counter(), defaultdict(float), []
        self._real = llm.call

    def __enter__(self):
        def audited(task, prompt, *a, **kw):
            sent = f"{kw.get('system') or ''}\n{prompt}".casefold()
            for n in self.needles:
                if n.casefold() in sent:
                    self.hits.append((task, n[:60]))
            self.requests[task] += 1
            r = self._real(task, prompt, *a, **kw)
            self.cost[task] += r.get("cost_usd") or 0.0
            return r
        llm.call = audited
        return self

    def __exit__(self, *exc):
        llm.call = self._real


def repos_from_resume(doc) -> list[str]:
    links = [ln.url for ln in (doc.structure.links if doc.structure else [])] + (doc.resume.contact_block.urls if doc.resume else [])
    out = []
    for u in links:
        m = _GH_REPO.search(u or "")
        if m and m.group(2) not in ("", "."):
            name = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
            if name not in out:
                out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", type=Path, required=True)
    ap.add_argument("--user", required=True, help="the candidate's GitHub username (as supplied by the operator)")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--email", action="append", default=[], help="extra commit emails (never shown in the report)")
    ap.add_argument("--reference", help="an opaque operator reference for the candidate (no emails)")
    ap.add_argument("--decomposition-tier", default="nano")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    logging.disable(logging.WARNING)
    run_id = uuid.uuid4().hex[:8]
    tag = f"crucible:{run_id}"

    doc = analyze_document(a.resume)  # 2C, zero model calls
    needles = [h.text for h in doc.hidden_fragments] + [f.reconstructed_text or "" for f in doc.security_findings]
    repos = a.repo or repos_from_resume(doc)
    if not repos:
        print("no --repo given and the resume links no GitHub repository: nothing to check claims against", file=sys.stderr)
        return 2

    with PromptAudit(needles) as audit:
        extraction = extract_claims(doc, decomposition_tier=a.decomposition_tier, run_tag=f"{tag}:4A")
        claims = extraction.claims or []

        report = analyze_targets(repos, user=a.user, emails=a.email or None, opts=Options())  # 3.6
        if a.email:
            identity = IdentitySet(login="")
            for e in a.email:
                identity.add(e, "admin_supplied_email")
        else:
            client = GitHubClient()
            metas = [client.get_repo(*t.repo_facts.full_name.split("/")) for t in report.targets if t.repo_facts]
            identity = build_identity_set(a.user, client, repos=metas)
        with tempfile.TemporaryDirectory(prefix="crucible-e2e-") as tmp:
            views, skipped = [], []
            for t in report.targets:
                if t.status not in ("resolved", "archived_ok") or t.repo_facts is None:
                    skipped.append(f"{t.raw_input}: {t.status} {t.message or ''}".strip())
                    continue
                checkout = Path(tmp) / t.repo_facts.full_name.replace("/", "__")
                subprocess.run(["git", "clone", "--quiet", f"https://github.com/{t.repo_facts.full_name}.git", str(checkout)],
                               check=False)
                views.append(retrieval.load_view(t, snapshot_root=cache.CACHE_DIR, checkout=checkout if checkout.exists() else None,
                                                 identity=identity))
            sets = retrieval.retrieve_all(claims, views)  # 4B
            run = verdicts.verdicts_for(claims, sets, run_tag=f"{tag}:4C")  # 4C
            grounded, failures = grounding.ground_all(run.verdicts, sets, views)  # 4D
            view_notes = [n for v in views for n in v.notes]

    rep = build_report(verdicts=grounded, evidence_sets=sets, target_reports=report.targets, extraction=extraction,
                       grounding_failures=failures, run=run, claims=claims,
                       candidate={"github_user": a.user, **({"reference": a.reference} if a.reference else {})},
                       run_label=f"End-to-end run {run_id}: resume {a.resume.name}, repositories {', '.join(repos)}.")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(rep.model_dump_json())
    a.out.with_suffix(".json").write_text(json.dumps(data, indent=1))
    a.out.write_text(render_html(data))

    # ---- console: table and accounting ----------------------------------------------------------------------------
    print(f"run {run_id}  resume={a.resume.name}  repos={repos}  page={a.out}")
    print(f"\n2C  status={doc.status}  hidden_fragments={len(doc.hidden_fragments)}  security_findings={len(doc.security_findings)}  "
          f"needs_review={doc.needs_review}")
    g = extraction.shield
    print(f"    Shield gate applied={g.applied}  units_removed={g.units_removed}  items={len(g.items)}")
    units = extraction.units or []
    by = Counter((e.stage, e.reason_code) for e in (extraction.excluded or []))
    print(f"4A  units={len(units)}  claims={len(claims)}  excluded_units={len(extraction.excluded or [])}  "
          f"stage4_dropped_claims={sum(len(u.dropped_claims) for u in units)}  truncated={extraction.truncated}")
    for (stage, code), n in sorted(by.items()):
        print(f"    dropped at {stage:<14} {code:<26} {n}")
    for u in units:
        for d in u.dropped_claims:
            print(f"    stage4 dropped claim {u.unit_id}: {d!r}")
    print(f"3.6 targets={len(report.targets)}  analysed={len(views)}  skipped={skipped}")
    nc = Counter(s.skip_reason for s in sets if not s.retrieval_performed)
    print(f"4B  searched={sum(1 for s in sets if s.retrieval_performed)}  not_checkable={sum(nc.values())}  "
          f"items={sum(len(s.items) for s in sets)}  notes={view_notes}")
    for why, n in nc.items():
        print(f"    not checkable x{n}: {why}")
    print(f"4C  requests={run.calls}  cost=${run.cost_usd}  not_judged={run.not_judged}  counters={dict(run.counters)}")
    print(f"4D  grounding_failures={dict(failures)}")
    print("\nmodel requests and cost by task:")
    for task in sorted(audit.requests):
        print(f"    {task:<22} requests={audit.requests[task]:<4} cost=${audit.cost[task]:.6f}")
    print(f"    TOTAL                  requests={sum(audit.requests.values()):<4} cost=${sum(audit.cost.values()):.6f}")
    print(f"\nprompt audit: {len(audit.needles)} needles (hidden spans + canary) checked in every prompt; hits={len(audit.hits)} {audit.hits}")
    print("\nCLAIMS")
    for r in rep.rows:
        c = r.citation
        cite = f"{c['repo']}:{c['path']}:{c['start_line']}-{c['end_line']}" if c else "-"
        print(f"  {r.claim_id:<8} {r.verdict:<13} [{r.claim_kind}] {r.claim_text!r}")
        print(f"           reason: {r.reason}")
        print(f"           basis: {r.matrix_row}  agreement={r.agreement} tier={r.model_tier} escalated={r.escalated}  cite: {cite}")
    return 3 if audit.hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
