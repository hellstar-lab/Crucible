"""Phase 4M M1: project / entry headings are references, not claims. Deterministic, no model call.

Measured: the project heading "Crucible, hiring-evidence system" reached decomposition, became "Built a Crucible,
hiring-evidence system" (a verb the resume never states) and came out SUPPORTED as an ownership claim. A false
supported is over-crediting, the worse direction.

Listing a project implies involvement only by convention, and supplying that unstated assumption is exactly what this
pipeline refuses to do elsewhere (no "proficient in" for a skills list, no qualifier the unit does not state). So a
heading or entry line under a Projects / Experience / Education section is a PROJECT REFERENCE: kept as context for
the bullets beneath it (it already is: heading_path and neighbouring context) and as a retrieval target (the report
says whether an analysed repository carries its name), but never itself a claim, never verified, never given a verb.

Not references: bullets; sentences (terminal punctuation, or a verb); and job-title lines ("Senior Engineer,
Northwind Payments"), which assert a held role and stay role_or_team claims (Phase 4J J4a; a claim the candidate made
must never vanish, Phase 4L L2).
"""

from __future__ import annotations

import re

from . import routing, verbs

ENTRY_SECTIONS = re.compile(r"(?i)\b(projects?|experience|employment|work history|education|open[- ]source|portfolio)\b")
MAX_ENTRY_WORDS = 16


def reference_kind(text: str, kind: str, section: str | None, heading_path: list[str] | None) -> str | None:
    """"entry_heading" / "entry_line" when the unit is a project reference, else None."""
    if not any(ENTRY_SECTIONS.search(h) for h in [section, *(heading_path or [])] if h):
        return None
    if kind == "bullet" or routing.is_title_line(text):
        return None
    if kind == "entry_heading":
        return "entry_heading"
    t = (text or "").strip()
    if not t or t.endswith((".", "!", "?")) or len(t.split()) > MAX_ENTRY_WORDS or verbs.has_verb(t, source=True):
        return None
    return "entry_line"
