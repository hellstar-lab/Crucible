"""Render a ClaimEvidenceReport JSON (Phase 4D output) as one self-contained HTML page.

    python scripts/render_report.py REPORT.json [--out reports/NAME.html] [--title "Claim evidence report"]

The output has no network dependencies (inline CSS, no fonts, no scripts) and opens from file://. The renderer formats
and never calculates: every number on the page is traced to a field of the input JSON, and the render fails if one is
not (backend/grounding/html.py, assert_traceable). Default output: reports/<input stem>.html.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.contracts import ClaimEvidenceReport  # noqa: E402
from backend.grounding.html import render_html  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--title", default="Claim evidence report")
    a = ap.parse_args()
    raw = json.loads(a.report.read_text())
    ClaimEvidenceReport.model_validate(raw)  # the contract, strictly (extra="forbid"): an unknown field is an error
    out = a.out or ROOT / "reports" / f"{a.report.stem}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(raw, a.title))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
