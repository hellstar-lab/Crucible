"""Phase 4A: document -> atomic, traceable, typed claims (extraction only, no verdicts).

    python scripts/extract_claims.py resume.pdf --decomposition-tier nano [--json out.json] [--cost-cap 0.005]

Stage 4's tier has no default: it is chosen from the labelled eval set (scripts/eval_extraction.py), so it must be
named. Live model calls through backend/llm.py; every call is logged with its cost and prompt hash.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.claims.pipeline import extract_claims  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--decomposition-tier", required=True, choices=["nano", "super", "ultra"])
    ap.add_argument("--disambiguation-tier", default=None, choices=["nano", "super", "ultra"], help="Stage 3 tier (default: config, super)")
    ap.add_argument("--stage3-escalation", default=None, choices=["ultra"], help="escalate Stage 3 when a guard catches a guess (off by default)")
    ap.add_argument("--cost-cap", type=float, default=None, help="USD per document (default: ~40 nano calls)")
    ap.add_argument("--json")
    a = ap.parse_args()
    kw = {"disambiguation_tier": a.disambiguation_tier} if a.disambiguation_tier else {}
    out = extract_claims(analyze_document(a.path), decomposition_tier=a.decomposition_tier, cost_cap_usd=a.cost_cap,
                         stage3_escalation=a.stage3_escalation, run_tag=f"extract:{Path(a.path).name}", **kw)
    if a.json:
        Path(a.json).write_text(out.model_dump_json(indent=2))
    print(f"{out.document}: shield gate applied={out.shield.applied} status={out.shield.document_status} detectors={out.shield.detectors} "
          f"hidden_fragments={out.shield.hidden_fragments}")
    if out.not_extracted_reason:
        print(f"not extracted: {out.not_extracted_reason}")
        return 1
    print(f"units={len(out.units)} claims={len(out.claims)} excluded={len(out.excluded)} calls={out.calls} cost=${out.cost_usd} "
          f"(cap ${out.cost_cap_usd}) truncated={out.truncated}")
    for c in out.claims:
        why = f" | {c.why_not_checkable}" if c.why_not_checkable else ""
        print(f"  CLAIM [{c.kind}, checkable={c.checkable_from_code}] {c.text}{why}")
    for e in out.excluded:
        print(f"  EXCLUDED [{e.stage}/{e.reason_code}] {out.document_text[e.source_span[0]:e.source_span[1]][:70]!r}: {e.detail[:140]}")
    print(f"counters={out.counters} disagreement_rate={out.sufficiency_disagreement_rate} key_order_rate={out.key_order_follows_schema_rate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
