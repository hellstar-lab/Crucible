"""Modality inheritance (Stage 1, item 1): a claim inherits the modality of its source. Deterministic, no model.

A unit is a NON-ASSERTION (extracted verbatim, shown as "stated as not yet done", never retrieved, never given a
verdict) when its source is:
  pending_heading   a section heading (or any heading above it) containing Pending / Planned / Future / Roadmap;
  future_lead_in    a section whose lead-in (its first unit) is future tense ("Upon completion, X will deliver ...");
                    every unit of the section inherits it, the lead-in included;
  status_cell       a table row whose status cell reads Pending / In progress / Scheduled / Under development
                    (a cell under a "Status" header, or a cell that is nothing but such a status);
  status_line       a "Label: <status> ..." line ("Load Benchmarking: Pending for heavy traffic burst scenarios."):
                    a status cell of a borderless key-value list. [an addition to the stated rules; reported apart]
  research_activity a unit that says someone IS studying / exploring / investigating / researching something ("Looking
                    ahead, we are studying MD-ACC methodologies"): not unfinished work but not knowable from code, so it
                    is never verified either; the reviewer is told exactly that. Checked before future_statement.
  gerund_fragment   a unit (or a Stage 4 claim) that is a gerund phrase with no subject and no finite verb ("Preventing
                    CPU saturation.", "4. Preventing Race Conditions and Overselling"): it names an activity, it does
                    not assert done work. Measured: decomposition produced "Preventing CPU saturation." from "... proved
                    difficult to prevent CPU saturation", which the tense guard then rejected for the wrong reason.
  future_statement  a unit that states a plan or the future itself ("will ...", "We plan to benchmark ...", "Moving
                    forward, ..."). [an addition to the stated rules; reported apart]
Measured on the OSDBMS phase-II report: pending tasks became "Developed ..." claims, and an expected deliverable
became "An automated concurrency adjuster scales throughput."; both were searched as done work.
"""

from __future__ import annotations

import re

PENDING_HEADING = re.compile(r"(?i)\b(pending|planned|future|roadmap)\b")
FUTURE = re.compile(r"(?i)\b(?:will|shall)\s+(?!not\b)[a-z]+|\bgoing to\b|\bupon completion\b|\b(?:plan|plans|planned|intend|intends|"
                    r"aim|aims|expect|expects|hope|hopes)\s+to\b|\bexpected\s+(?:to|outputs?|deliverables?)\b|\blooking ahead\b|"
                    r"\bmoving forward\b|\bnext steps?\b")
RESEARCH = re.compile(r"(?i)\b(?:we|i|they|the team|our team)\s+(?:are|am|is|were|was|have been|has been)\s+(?:currently\s+|also\s+|now\s+)?"
                      r"(studying|exploring|investigating|researching)\b")
STATUS = r"(?:pending|in[\s-]progress|scheduled|under development|currently being|not (?:yet )?started|to[\s-]?do|planned)"
STATUS_VALUE = re.compile(rf"(?i)^\s*{STATUS}\b")
STATUS_LINE = re.compile(rf"(?i)^\s*[A-Z][^:;]{{1,60}}:\s*{STATUS}\b")
_SPACE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _SPACE.sub(" ", s or "").strip()


def _status_cell(row) -> str | None:
    for i, cell in enumerate(row.cells):
        head = (row.header[i] if row.header and i < len(row.header) else "") or ""
        if re.search(r"(?i)\bstatus\b", head) and STATUS_VALUE.match(cell or ""):
            return f"{head.strip()}: {cell.strip()}"
        if STATUS_VALUE.match(cell or "") and len((cell or "").split()) <= 3:
            return cell.strip()
    return None


def classify(units, tables=None) -> dict[str, tuple[str, str]]:
    """{unit_id: (rule, detail)} for every unit that is a non-assertion. ``units`` in document order (split.Unit);
    ``tables``: the 2C tables, whose ``row_lines`` are the table rows as written into the section text."""
    rows = [rl for t in (tables or []) for rl in (getattr(t, "row_lines", None) or [])]
    out: dict[str, tuple[str, str]] = {}
    lead: dict[str | None, object] = {}
    for u in units:
        lead.setdefault(u.section, u)  # the section's first unit is its lead-in
    for u in units:
        heads = [h for h in [u.section, *(u.heading_path or [])] if h]
        hit = next((h for h in heads if PENDING_HEADING.search(h)), None)
        if hit:
            out[u.unit_id] = ("pending_heading", f"under the heading {hit!r}")
            continue
        li = lead.get(u.section)
        if li is not None and FUTURE.search(li.text or ""):
            out[u.unit_id] = ("future_lead_in", f"the section {u.section!r} opens in the future tense: {_norm(li.text)[:120]!r}")
            continue
        text = _norm(u.text)
        row = next((r for r in rows if text and text in _norm(r.text)), None)
        status = _status_cell(row) if row is not None else None
        if status:
            out[u.unit_id] = ("status_cell", f"a table row whose status cell reads {status!r}")
            continue
        if STATUS_LINE.match(text):
            out[u.unit_id] = ("status_line", f"a status line: {text[:120]!r}")
            continue
        if gerund_fragment(text):
            out[u.unit_id] = ("gerund_fragment", "a gerund phrase with no subject and no finite verb: it names an activity, "
                                                 "it does not state that it was done")
            continue
        r = RESEARCH.search(text)
        if r:
            out[u.unit_id] = ("research_activity", f"describes research activity ({r.group(0)!r}), which is not knowable "
                                                   "from code")
            continue
        m = FUTURE.search(text)
        if m:
            out[u.unit_id] = ("future_statement", f"states a plan or the future ({m.group(0)!r})")
    return out


TEST_RESULT = re.compile(r"(?i)^\s*(pass(?:ed|es|ing)?|fail(?:ed|s|ing)?|ok|success(?:ful)?|green|red|broken)\b")


def test_status(units, tables=None) -> dict[str, str]:
    """{unit_id: the reported result} for units from a table row whose STATUS cell reports a test result ("Pass").
    Stage 2: such a row is routed to inconclusive at 4C. A pass or a fail is the outcome of RUNNING the test, which a
    repository does not record: the test file can be found, its result cannot. (A pending status is a non-assertion.)"""
    rows = [rl for t in (tables or []) for rl in (getattr(t, "row_lines", None) or [])]
    out = {}
    for u in units:
        text = _norm(u.text)
        row = next((r for r in rows if text and text in _norm(r.text)), None)
        if row is None:
            continue
        for i, cell in enumerate(row.cells):
            head = (row.header[i] if row.header and i < len(row.header) else "") or ""
            if re.search(r"(?i)\b(status|result)\b", head) and TEST_RESULT.match(cell or ""):
                out[u.unit_id] = cell.strip()
                break
    return out


def row_units(units, tables=None) -> set[str]:
    """Units that are reconstructed table rows (or part of one)."""
    rows = [_norm(rl.text) for t in (tables or []) for rl in (getattr(t, "row_lines", None) or [])]
    return {u.unit_id for u in units if _norm(u.text) and any(_norm(u.text) in r for r in rows)}


def gerund_fragment(text: str) -> bool:
    """A gerund phrase with no subject and no finite verb ("Preventing CPU saturation."). The same lexicon-free reading
    as the tense guard's verbing_fragment construction (backend.claims.tense): an opening -ing word that is not an
    -ing adjective ("Incoming portal requests route properly"), no comma, no auxiliary, no past form."""
    from . import tense
    return tense.verbing_fragment(text) is not None


