"""names_another_person (Stage 1, item 3). NOT team mode: no per-person reports, no reconciliation, no team flag.

A table row can say WHO did the work ("Task Completed: ...; Team Member: Rathi, Kasak."). The name stays attached to
every claim from that row (``attributed_to``). When the row names a person other than the candidate, the unit is
excluded before any model call with reason ``names_another_person``: no retrieval, no verdict; the reviewer sees the
row with the name.

The candidate is the operator's --email. Their name is read from the document itself: the table cell or line that
carries that email ("Bansal, Abhinav - 24021246 abhinavbansal2120@ gmail.com" -> "Bansal, Abhinav"). When it cannot
be found, a row naming a person is NEVER assumed to be the candidate's: it is excluded as ``names_unresolved`` with
the name shown, none of them is verified, and the report says so. A group ("All team members") includes the candidate
and is kept.
"""

from __future__ import annotations

import re

PERSON_HEADER = re.compile(r"(?i)\b(team member|member|owner|assignee|author|done by|responsible|person|contributor|who)\b")
_NAME = re.compile(r"^\s*([A-Z][A-Za-z'.-]+(?:,\s*|\s+)[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+)?)")
_LAST_FIRST = re.compile(r"^[A-Z][A-Za-z'.-]+,\s*[A-Z][A-Za-z'.-]+$")
GROUP = re.compile(r"(?i)^\s*(all\b|everyone|the (whole )?team|team\b|whole team|all team members)")
_WS = re.compile(r"\s+")


def _squash(s: str) -> str:
    return _WS.sub("", (s or "").lower())


def candidate_name(structure, emails: list[str] | None) -> str | None:
    """The candidate's name as the document writes it, from the cell / line that carries one of their emails."""
    want = [_squash(e) for e in (emails or []) if e and "@" in e]
    if not want or structure is None:
        return None
    texts = [c for t in (structure.tables or []) for row in t.rows for c in row]
    texts += [ln for s in (structure.sections or []) for ln in (s.text or "").split("\n")]
    for text in texts:
        sq = _squash(text)
        for e in want:
            if e in sq:
                # the text before the email, with the email's own pieces removed (it may be split by a line break)
                head = text[: max(0, text.lower().find(e.split("@")[0]))] if e.split("@")[0] in text.lower() else text
                m = _NAME.match(head)
                if m:
                    return " ".join(m.group(1).split())
    return None


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", (name or "").lower()) if len(t) > 1}


def same_person(a: str, b: str) -> bool:
    """"Bansal, Abhinav" == "Abhinav Bansal": the same name tokens, in any order (one may add a middle name)."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def row_person(row) -> str | None:
    """The person a table row attributes its work to: a cell under a person header, or a cell that is a bare
    "Last, First" name."""
    for i, cell in enumerate(row.cells):
        head = (row.header[i] if row.header and i < len(row.header) else "") or ""
        value = " ".join((cell or "").split())
        if not value:
            continue
        if PERSON_HEADER.search(head) or _LAST_FIRST.match(value):
            return value
    return None


def attribute(units, tables) -> dict[str, str]:
    """{unit_id: the person the unit's table row names}."""
    rows = [rl for t in (tables or []) for rl in (getattr(t, "row_lines", None) or [])]
    norm = lambda s: " ".join((s or "").split())  # noqa: E731
    out = {}
    for u in units:
        text = norm(u.text)
        row = next((r for r in rows if text and text in norm(r.text)), None)
        person = row_person(row) if row is not None else None
        if person:
            out[u.unit_id] = person
    return out


def names_another(person: str, candidate: str | None) -> bool:
    """True when ``person`` is someone other than the candidate. A group ("All team members") includes the
    candidate; an unknown candidate excludes nobody."""
    if not candidate or GROUP.match(person or ""):
        return False
    return not same_person(person, candidate)
