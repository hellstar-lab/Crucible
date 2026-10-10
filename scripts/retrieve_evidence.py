"""Phase 4B: retrieve repository evidence for extracted claims. Deterministic: no model calls, no network.

    python scripts/retrieve_evidence.py --claims claims.json --evidence repos.json --json out.json \\
        [--user LOGIN] [--email ADDR ...] [--checkout owner/repo=PATH ...] [--snapshot-root DIR]

--claims    a 4A ClaimExtraction JSON (scripts/extract_claims.py --json) or a JSON list of claims
--evidence  a 3.6 AnalysisReport JSON (scripts/analyze_repos.py --json), or a list of TargetReports
--user / --email   the candidate identity. The reports store emails redacted, so the identity is passed again; per-file
            authorship is recomputed from the cached snapshot and checked against the report's totals.
--checkout  local clone of a repository, used ONLY for verbatim excerpts (git show <head_sha>:<path>)
--snapshot-root    the 3.6 snapshot cache (default .cache/evidence)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import retrieval  # noqa: E402
from backend.contracts import TargetReport  # noqa: E402
from backend.evidence.identity import IdentitySet  # noqa: E402


def _claims(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data.get("claims") or [] if isinstance(data, dict) else data


def _reports(path: Path) -> list[TargetReport]:
    data = json.loads(path.read_text())
    targets = data.get("targets", []) if isinstance(data, dict) else data
    return [TargetReport.model_validate(t) for t in targets]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claims", required=True, type=Path)
    ap.add_argument("--evidence", required=True, type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--user")
    ap.add_argument("--email", action="append", default=[])
    ap.add_argument("--checkout", action="append", default=[], help="owner/repo=PATH (repeatable)")
    ap.add_argument("--snapshot-root", type=Path, default=None)
    a = ap.parse_args()

    identity = None
    if a.user or a.email:
        identity = IdentitySet(login=a.user or "")
        for e in a.email:
            identity.add(e, "cli_email")
    checkouts = dict(c.split("=", 1) for c in a.checkout)
    views = []
    for rep in _reports(a.evidence):
        if rep.status not in ("resolved", "archived_ok") or rep.repo_facts is None:
            print(f"skip {rep.raw_input}: status {rep.status}")
            continue
        co = checkouts.get(rep.repo_facts.full_name)
        views.append(retrieval.load_view(rep, snapshot_root=a.snapshot_root, checkout=Path(co) if co else None, identity=identity))
    sets = retrieval.retrieve_all(_claims(a.claims), views)
    for es in sets:
        if not es.retrieval_performed:
            print(f"{es.claim_id:<12} {es.claim_kind:<16} NOT SEARCHED: {es.skip_reason}")
            continue
        authored = sum(1 for i in es.items if i.authored_by_candidate is True)
        print(f"{es.claim_id:<12} {es.claim_kind:<16} rules={','.join(es.searched.rules_fired)} items={len(es.items)} "
              f"(+{es.items_dropped} dropped) authored_by_candidate={authored} technologies={es.searched.technologies}")
    for note in dict.fromkeys(n for es in sets for n in es.notes):
        print(f"note: {note}")
    if a.json:
        a.json.write_text(json.dumps([es.model_dump() for es in sets], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
