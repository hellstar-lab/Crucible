"""Stage 1: split the 2C structure block into units and attach context. Deterministic, no model.

Claimify was built for prose. A resume is mostly bullet FRAGMENTS ("Built Redis caching layer serving 12k req/s":
no subject, often no full stop). Rules:

  * a line that starts with a bullet marker starts a new bullet unit, with or without terminal punctuation;
  * two bullets are never merged because neither ends in a full stop: the next marker is the boundary;
  * a line without a marker that follows a bullet CONTINUES it (a wrapped bullet) when it starts in lower case or
    the bullet ends on a word that cannot end a phrase (and, of, with, a comma, a hyphen ...);
  * a bullet containing two sentences becomes two units that share ``bullet_id``;
  * a sub-bullet (a different marker glyph inside a list) is its own unit with ``parent_bullet_id``, and its
    parent's text is part of its context;
  * prose paragraphs are split into sentences.

Every unit carries ``source_span`` = [start, end) offsets into ``document_text`` (the cleaned text the 2C analyzer
extracted, rebuilt from its structure block) and ``source_section`` (the heading it sits under). The span text,
with bullet markers and whitespace normalised, equals the unit text; a test enforces this for every unit.

Contact lines (email / phone / URL) in the header are excluded here, so contact details never reach a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.contracts import ClaimUnit, StructureBlock

MARKER = re.compile(r"^\s*(?P<m>[•·▪●◦○■□‣⁃∙*–-]|\d{1,2}[.)])\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")
_ABBREV = re.compile(r"(?i)(?:\b(?:e\.g|i\.e|etc|vs|approx|inc|ltd|corp|co|jr|sr|dr|mr|ms|mrs|prof|no|fig|st|u\.s|b\.sc|m\.sc|b\.s|m\.s|"
                     r"ph\.d|b\.a|m\.a|b\.tech|m\.tech|b\.e|m\.e|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.|\b[A-Z]\.)$")
# "Engineer, Contoso Freight Jul 2018 - Feb 2021": a role line ending in a date range. The 2C analyzer sometimes
# appends one to the previous bullet's paragraph; it is a fact line of its own, not part of the bullet.
_ROLE_LINE = re.compile(r"(?i)(?:\b(?:19|20)\d{2}|present|current)\s*$")
_CONTINUES = re.compile(r"(?i)(?:[,;:/&+-]|\b(?:and|or|of|with|to|for|the|a|an|in|on|at|by|from|using|via|into|across|that|which|while))$")
_CONTACT = re.compile(r"(?i)[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{7,}\d|https?://|www\.|github\.com/|linkedin\.com/")
CANONICAL_SECTION_WORDS = re.compile(r"(?i)^(summary|profile|experience|work experience|professional experience|employment|education|"
                                     r"skills|technical skills|projects|certifications?|publications|awards|interests|languages)$")


@dataclass
class Unit:
    unit_id: str
    text: str
    start: int
    end: int
    section: str | None
    heading_path: list[str]
    kind: str
    bullet_id: str | None = None
    parent_bullet_id: str | None = None
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    parent_text: str | None = None

    def to_contract(self) -> ClaimUnit:
        return ClaimUnit(unit_id=self.unit_id, text=self.text, source_span=(self.start, self.end), source_section=self.section,
                         heading_path=self.heading_path, kind=self.kind, bullet_id=self.bullet_id, parent_bullet_id=self.parent_bullet_id)

    def context(self) -> str:
        parts = []
        if self.heading_path:
            parts.append("Headings: " + " > ".join(self.heading_path))
        if self.parent_text:
            parts.append("Parent bullet: " + self.parent_text)
        if self.context_before:
            parts.append("Before: " + " | ".join(self.context_before))
        if self.context_after:
            parts.append("After: " + " | ".join(self.context_after))
        return "\n".join(parts)


def rebuild_text(block: StructureBlock) -> tuple[str, list[tuple[int, int, str, str | None, int | None, bool]]]:
    """(document_text, lines). The 2C analyzer's full text is "\\n".join(units) with each section = heading unit +
    paragraph units, and Section.char_start/char_end index into it; this rebuilds it exactly from the block.
    lines: (start, end, text, section heading, heading level, is_heading)."""
    parts: list[str] = []
    lines = []
    pos = 0
    for sec in block.sections:
        rows = ([(sec.heading, True)] if sec.heading is not None else []) + [(p, False) for p in (sec.text.split("\n") if sec.text else [])]
        for text, is_heading in rows:
            lines.append((pos, pos + len(text), text, sec.heading, sec.level, is_heading))
            parts.append(text)
            pos += len(text) + 1
    return "\n".join(parts), lines


def _clean(text: str) -> str:
    return " ".join(MARKER.sub("", text, count=1).split())


def _sentences(text: str, start: int) -> list[tuple[int, int]]:
    """Sentence boundaries inside [start, start+len(text)), skipping abbreviations and decimals."""
    out, s = [], 0
    for m in _SENT_SPLIT.finditer(text):
        if _ABBREV.search(text[:m.start()]):
            continue
        out.append((start + s, start + m.start()))
        s = m.end()
    out.append((start + s, start + len(text)))
    return [(a, b) for a, b in out if text[a - start:b - start].strip()]


def split_lines(lines: list[tuple[int, int, str]], document_text: str, section: str | None = None,
                heading_path: list[str] | None = None, id_prefix: str = "u") -> list[Unit]:
    """Units from consecutive paragraph lines of ONE section. Each line is (start, end, text) in document_text."""
    heading_path = heading_path or []
    groups: list[dict] = []  # each: {start, end, bullet: bool, glyph, level}
    list_glyph = None
    for start, end, text in lines:
        if not text.strip():
            continue
        m = MARKER.match(text)
        if m:
            glyph = m.group("m")
            glyph = "#" if glyph[0].isdigit() else glyph
            if list_glyph is None:
                list_glyph = glyph
            level = 1 if glyph == list_glyph else 2
            groups.append({"start": start, "end": end, "bullet": True, "level": level})
            continue
        prev = groups[-1] if groups else None
        prev_text = document_text[prev["start"]:prev["end"]].rstrip() if prev else ""
        if prev and prev["bullet"] and (text.lstrip()[:1].islower() or _CONTINUES.search(prev_text)):
            prev["end"] = end  # a wrapped bullet: same unit, span covers both lines
            continue
        list_glyph = None if not prev or not prev["bullet"] else list_glyph
        groups.append({"start": start, "end": end, "bullet": False, "level": 0})

    units: list[Unit] = []
    n = 0
    last_parent: tuple[str, str] | None = None
    for gi, g in enumerate(groups):
        raw = document_text[g["start"]:g["end"]]
        lead = MARKER.match(raw)
        body_start = g["start"] + (lead.end() if lead else 0)
        body = document_text[body_start:g["end"]]
        bullet_id = f"{id_prefix}b{gi}" if g["bullet"] else None
        parent_id, parent_text = (last_parent if g["bullet"] and g["level"] == 2 and last_parent else (None, None))
        for a, b in _sentences(body, body_start):
            text = _clean(document_text[a:b])
            if not text:
                continue
            kind = "bullet" if g["bullet"] else ("sentence" if re.search(r"[.!?]$", text) or len(text.split()) > 8 else "line")
            in_bullet = bullet_id
            if g["bullet"] and a > body_start and _ROLE_LINE.search(text) and not re.search(r"[.!?]$", text):
                kind, in_bullet = "line", None  # a role/date line glued onto a bullet by the analyzer
            units.append(Unit(f"{id_prefix}{n}", text, a, b, section, list(heading_path), kind, bullet_id=in_bullet,
                              parent_bullet_id=parent_id if in_bullet else None, parent_text=parent_text if in_bullet else None))
            n += 1
        if g["bullet"] and g["level"] == 1:
            last_parent = (bullet_id, _clean(raw))
    for i, u in enumerate(units):
        u.context_before = [x.text for x in units[max(0, i - 2):i]]
        u.context_after = [x.text for x in units[i + 1:i + 2]]
    return units


def split_structure(block: StructureBlock) -> tuple[str, list[Unit], list[Unit]]:
    """(document_text, units, contact_units). Section headings are context, not units, except entry headings
    (a level-3+ heading such as "Senior Engineer, Northwind Payments  Mar 2021 - Present") which state facts."""
    document_text, lines = rebuild_text(block)
    units: list[Unit] = []
    contact: list[Unit] = []
    stack: list[tuple[int, str]] = []
    pending: list[tuple[int, int, str]] = []
    cur_section: str | None = None
    first_heading_seen = False
    sec_index = 0

    def flush():
        nonlocal pending, sec_index
        if pending:
            for u in split_lines(pending, document_text, cur_section, [h for _l, h in stack], id_prefix=f"s{sec_index}u"):
                (contact if not first_canonical_seen and _CONTACT.search(u.text) else units).append(u)
            sec_index += 1
        pending = []

    first_canonical_seen = False
    for start, end, text, heading, level, is_heading in lines:
        if is_heading:
            flush()
            lvl = level or 1
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            if first_heading_seen and lvl >= 3 and not CANONICAL_SECTION_WORDS.match(text.strip()):
                u = Unit(f"h{len(units)}", " ".join(text.split()), start, end, heading, [h for _l, h in stack], "entry_heading")
                (contact if not first_canonical_seen and _CONTACT.search(u.text) else units).append(u)
            stack.append((lvl, text.strip()))
            first_heading_seen = True
            if CANONICAL_SECTION_WORDS.match(text.strip()):
                first_canonical_seen = True
            cur_section = heading
            continue
        pending.append((start, end, text))
    flush()
    for i, u in enumerate(units):  # neighbouring context across the whole section order
        if not u.context_before and i > 0 and units[i - 1].section == u.section:
            u.context_before = [units[i - 1].text]
    return document_text, units, contact
