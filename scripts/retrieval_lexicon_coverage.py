"""Measure the 4B technology->artifact lexicon against the technologies the fixtures actually name.

    python scripts/retrieval_lexicon_coverage.py

Claim technologies = every technology the shield lexicon (backend/shield/data/tech_lexicon.txt) recognises in the
resume fixtures, the project-report fixture, the local external CV corpus (if present) and the extraction eval set.
Reports how many distinct technologies the 4B lexicon knows, how many of those have a code artifact, and the claim
technologies it does NOT know. The unknown list is a measured gap, not a hidden one.
"""

from __future__ import annotations

import glob
import json
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.claims import support  # noqa: E402
from backend.retrieval import lexicon  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402


def fixture_texts() -> dict[str, str]:
    logging.disable(logging.CRITICAL)
    docs = sorted(glob.glob(str(ROOT / "tests/fixtures/docs/resume_*.pdf"))) + [str(ROOT / "tests/fixtures/docs/docx_clean_resume.docx"),
                                                                                 str(ROOT / "tests/fixtures/docs/report_project.pdf")]
    docs += sorted(glob.glob(str(ROOT / ".cache/external_corpus/*.pdf")))
    out = {}
    for d in docs:
        rep = analyze_document(d)
        if rep.structure:
            out[Path(d).name] = "\n".join([s.text for s in rep.structure.sections] + [h.text for h in rep.structure.headings])
    for line in (ROOT / "tests/eval/extraction_set.jsonl").read_text().splitlines():
        row = json.loads(line)
        out[f"eval:{row['id']}"] = row["input_text"] + "\n" + "\n".join(row["context"].get("before", []))
    return out


def main() -> int:
    table, _norm = support._lexicon()
    canon = {c for c, _a in table.values()}
    freq: Counter = Counter()
    for text in fixture_texts().values():
        freq.update({e for e in support.entities(text) if e in canon})
    known = lexicon.technologies()
    alias_to_key = {a.lower(): k for k, v in known.items() for a in v["aliases"] + [k]}
    unknown = sorted((t for t in freq if t.lower() not in alias_to_key), key=lambda t: (-freq[t], t))
    no_artifact = sorted(k for k, v in known.items() if not v.get("code_artifacts", True))
    print(f"4B lexicon: {len(known)} technologies ({len(known) - len(no_artifact)} with a code artifact; "
          f"{len(no_artifact)} known to leave none: {no_artifact})")
    print(f"claim technologies in the fixtures: {len(freq)} distinct")
    print(f"covered: {len(freq) - len(unknown)} / {len(freq)}")
    print(f"NOT known to the 4B lexicon ({len(unknown)}): {[(t, freq[t]) for t in unknown]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
