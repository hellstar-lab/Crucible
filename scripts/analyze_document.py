"""Analyze one PDF or DOCX: security findings, provenance, structure and (for resumes) the resume block.

    python scripts/analyze_document.py resume.pdf            # summary
    python scripts/analyze_document.py resume.pdf --json     # full DocumentReport
    python scripts/analyze_document.py scan.pdf --ocr        # OCR image-only pages (needs local tesseract)

Deterministic and offline: no model calls, no network. The document is never executed and no link
or attachment in it is followed. Exit code: 0 clean, 1 suspicious / hidden_text_found / needs review.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.shield import ShieldError  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true", help="print the full DocumentReport as JSON")
    ap.add_argument("--ocr", action="store_true", help="OCR image-only pages with local tesseract (default off)")
    ap.add_argument("--today", help="YYYY-MM-DD used for 'Present' dates (default: today)")
    a = ap.parse_args()
    try:
        rep = analyze_document(a.path, ocr=a.ocr, today=date.fromisoformat(a.today) if a.today else None)
    except (ShieldError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(rep.model_dump_json(indent=2))
    else:
        d, q = rep.document, rep.extraction_quality
        print(f"{d.path}  [{d.format}]  status={rep.status}  needs_review={rep.needs_review}")
        print(f"pages={d.pages}/{d.pages_total}  kinds={d.per_page_kind}  resume_confidence={d.resume_confidence}")
        print(f"hidden fragments={len(rep.hidden_fragments)}  findings={[(f.detector, f.severity) for f in rep.security_findings]}")
        if rep.structure:
            s = rep.structure
            print(f"headings={len(s.headings)} sections={len(s.sections)} tables={None if s.tables is None else len(s.tables)} "
                  f"links={len(s.links)} numeric_claims={len(s.numeric_claims)} references={len(s.references.entries)}")
        if rep.resume:
            r = rep.resume
            print(f"resume: {len(r.timeline)} dated entries, {len(r.timeline_contradictions)} contradictions, {len(r.skills_mentioned)} skills")
            for c in r.timeline_contradictions:
                print(f"  - {c.kind}: {c.detail} {c.cites}")
        for w in rep.warnings + [f"detector error: {e}" for e in rep.detector_errors]:
            print(f"warning: {w}")
        print(f"chars={q.chars_total} unmapped_ratio={q.chars_unmapped_ratio} ocr_required_pages={q.ocr_required_pages}")
    return 0 if rep.status == "clean" and not rep.needs_review else 1


if __name__ == "__main__":
    raise SystemExit(main())
