"""Skills lists: a deterministic decomposition path, no model call (Phase 4J J1).

Measured: both end-to-end resumes' Skills lines ("Python, Go, PostgreSQL, Docker, Kubernetes") were dropped at Stage 4
as "no verifiable claims", although they are the most code-checkable lines on the page. And a model asked to turn a
bare list into sentences wrote "Proficient in Python": a qualifier the resume never stated.

So a delimited list under a Skills-like heading (or behind a Skills-like label, "Skills: Python, Docker") is split
here, in code:
  * delimiters: , ; | and the bullet glyphs 2C recognises, at parenthesis depth 0 ("Python (Django, Flask)" stays one
    item);
  * a leading category label is stripped ("Languages: Python, Go" -> Python, Go);
  * each item becomes ONE claim whose text is the bare item, verbatim: no "proficient in", no "experience with";
  * kind technology_use, scope none, checkable_from_code yes; source_span points at the item itself;
  * an item missing from the tech lexicon is still a claim: 4B reports it as unknown_technology, so lexicon gaps stay
    visible instead of disappearing;
  * at most MAX_ITEMS items per unit; the overflow is recorded, never silently dropped.
A unit that does not look like a list (an item longer than MAX_ITEM_WORDS words) is left to the model path.
"""

from __future__ import annotations

import re

MAX_ITEMS = 40
MAX_ITEM_WORDS = 5
SKILLS_WORDS = r"(?:technical\s+)?(?:skills?|technolog(?:y|ies)|tools?|languages?|frameworks?|(?:tech\s+)?stack|toolkit)"
SKILLS_HEADING = re.compile(rf"(?i)\b{SKILLS_WORDS}\b")
# a leading category label: "Skills:", "Languages:", "Cloud & DevOps:" (any short label ending in a colon)
_LABEL = re.compile(r"^\s*([A-Za-z][\w &/+.-]{0,30}?)\s*:\s*")
_DELIMS = set(",;|•·▪●◦■□‣⁃∙")
_LEADING_JOIN = re.compile(r"(?i)^(?:and|or|&)\s+")


def is_skills_unit(text: str, section: str | None, heading_path: list[str] | None) -> bool:
    headings = [h for h in ([section] + list(heading_path or [])) if h]
    if any(SKILLS_HEADING.search(h) for h in headings):
        return True
    m = _LABEL.match(text or "")
    return bool(m and SKILLS_HEADING.fullmatch(m.group(1).strip()))


def split_items(document_text: str, start: int, end: int) -> list[tuple[int, int, str]] | None:
    """[(item_start, item_end, item_text)] with offsets into document_text, or None when the span is not a list."""
    raw = document_text[start:end]
    m = _LABEL.match(raw)
    off = m.end() if m else 0
    pieces, depth, s = [], 0, off
    for i in range(off, len(raw)):
        ch = raw[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch in _DELIMS and depth == 0:
            pieces.append((s, i))
            s = i + 1
    pieces.append((s, len(raw)))
    items = []
    for a, b in pieces:
        seg = raw[a:b]
        lead = len(seg) - len(seg.lstrip())
        a, seg = a + lead, seg.strip()
        j = _LEADING_JOIN.match(seg)
        if j:
            a, seg = a + j.end(), seg[j.end():]
        seg = seg.rstrip(" .")
        if not seg:
            continue
        if len(seg.split()) > MAX_ITEM_WORDS:
            return None  # prose under a Skills heading: not a list, leave it to the model path
        items.append((start + a, start + a + len(seg), " ".join(seg.split())))
    return items or None
