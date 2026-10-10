"""P2-3 / P2-6: Stage 3 ensemble agreement per tier, and semantic entropy over the samples. Live calls.

    python scripts/probe_stage3_ensemble.py [--repeats 2] [--json out.json]

Units: eval row w3 ("Improved it by 40%") and the two resume_standard.pdf units that Stage 3 wrongly refused in round 1
("Engineer, Contoso Freight Jul 2018 - Feb 2021" and "payments-api: open-source settlement service, gRPC and
PostgreSQL."), each with its real context. Those two are now skipped by the pre-gate; they are sent to Stage 3 here on
purpose, to measure agreement. One request per unit and tier: n=3, temperature 0.7.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.claims import ensemble, nli, stages  # noqa: E402
from backend.claims.split import split_lines, split_structure  # noqa: E402

TIERS = ["nano", "super", "ultra"]


def units() -> list[tuple[str, str, str]]:
    from backend.shield.analyze import analyze_document
    logging.disable(logging.CRITICAL)
    rep = analyze_document(ROOT / "tests" / "fixtures" / "docs" / "resume_standard.pdf")
    logging.disable(logging.NOTSET)
    _t, us, _c = split_structure(rep.structure)
    pick = {u.text: u for u in us}
    out = []
    for text in ("Engineer, Contoso Freight Jul 2018 - Feb 2021", "payments-api: open-source settlement service, gRPC and PostgreSQL."):
        out.append((f"resume:{text[:24]}", text, pick[text].context()))
    row = next(json.loads(line) for line in (ROOT / "tests/eval/extraction_set.jsonl").read_text().splitlines() if '"w3_' in line)
    lines = [(0, len(row["input_text"]), row["input_text"])]
    u = split_lines(lines, row["input_text"], section=row["context"]["headings"][-1], heading_path=row["context"]["headings"])[0]
    u.context_before = list(row["context"]["before"]) + u.context_before
    out.insert(0, ("w3", u.text, u.context()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--json")
    a = ap.parse_args()
    equivalent = nli.equivalent if nli.available() else None
    print(f"NLI available: {equivalent is not None}")
    rows = []
    for tier in TIERS:
        for uid, text, ctx in units():
            for rep in range(1, a.repeats + 1):
                r = stages.disambiguate_ensemble(text, ctx, tier, run_tag=f"P2-3:{uid}:{tier}:{rep}")
                cl = ensemble.clusters(r.samples, equivalent) if equivalent else None
                se = ensemble.semantic_entropy(cl) if cl else None
                rows.append({"tier": tier, "unit": uid, "repeat": rep, "decision": r.verdict, "unanimous": r.verdict != "cannot_be_disambiguated"
                             or r.data.get("reason") != "resolutions disagreed", "reason": r.data.get("reason"),
                             "samples": r.samples, "clusters": cl, "semantic_entropy": se, "cost_usd": r.cost_usd})
                print(f"{tier:<6}{uid:<34}rep{rep} decision={r.verdict:<24} SE={se}  samples={[(s['verdict'], s['clarified_text']) for s in r.samples]}")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
