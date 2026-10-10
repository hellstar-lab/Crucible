"""Resume-specific layer: contact block, timeline, deterministic contradictions, skills.

Only runs when the document looks like a resume (>= 3 canonical section headings). Nothing here
judges: a contradiction is two raw strings from the document that cannot both be true, cited
verbatim, and a date that cannot be parsed stays a raw string with start/end None (never invented).

Why dateparser is fenced: left alone it reads "Jun'22" as 22 June of the CURRENT year, "13/2020" as
13 October 2020 and a bare "2021" as October 2021 (it borrows the month from its reference date). So
apostrophe years, bare years and seasons are parsed here; month-name and MM/YYYY tokens go to
dateparser only after their month has been validated, and its answer must agree with the token.

Phone numbers are extracted only when they carry a country code: a bare local number cannot be
attributed to a country without guessing (the same digits are valid in several).

Logging never prints contact values; use ``redact``.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime
from pathlib import Path

import dateparser
import phonenumbers
from rapidfuzz import fuzz, process

from backend.contracts import (Contradiction, ContactBlock, ResumeBlock, SkillMention, TimelineEntry)

from . import config

log = logging.getLogger(__name__)
LEXICON_PATH = Path(__file__).resolve().parent / "data" / "tech_lexicon.txt"

CANONICAL_HEADINGS = {
    "experience": {"experience", "work experience", "professional experience", "employment", "employment history", "work history",
                   "career history", "relevant experience", "experience and projects"},
    "education": {"education", "academic background", "education and training", "academic qualifications", "academics"},
    "skills": {"skills", "technical skills", "key skills", "core skills", "skills and tools", "technologies", "technical proficiencies",
               "core competencies", "skills and technologies"},
    "projects": {"projects", "personal projects", "selected projects", "academic projects", "open source", "open source projects"},
    "certifications": {"certifications", "certificates", "licenses and certifications", "licences and certifications", "certifications and licenses"},
    "publications": {"publications", "selected publications"},
    "summary": {"summary", "professional summary", "profile", "about me", "career summary", "objective", "career objective"},
}
_PHRASE_TO_KIND = {p: k for k, ps in CANONICAL_HEADINGS.items() for p in ps}
SKILLS_LIKE = re.compile(r"(?i)skill|technolog|tools|stack|proficienc|competenc")
PART_TIME = re.compile(r"(?i)\b(intern(?:ship)?|part[- ]time|freelanc\w*|contract(?:or)?|consult(?:ant|ing)|volunteer\w*|apprentice\w*|co-?op|seasonal)\b")
CLAIMED_YEARS = re.compile(r"(?i)\b(\d{1,2})\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:[a-z-]+\s+){0,2}experience")
CREDENTIALS = {"phd": r"\b(ph\.?\s?d\.?|doctorate|doctoral)\b", "mba": r"\bm\.?b\.?a\.?\b", "md": r"\bm\.?d\.?\b", "jd": r"\bj\.?d\.?\b"}
CONTRADICTION_TOLERANCE_YEARS = 1.0
OVERLAP_MIN_DAYS = 31

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9
_SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_TOKEN = (rf"(?:{_MON}\.?\s*(?:['’]\d{{2}}|\d{{4}})|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{1,2}}|(?:spring|summer|fall|autumn|winter)\s+\d{{4}}|"
          rf"(?<![\d.,/-])(?:19|20)\d{{2}}(?![\d/-]))")
_PRESENT = r"present|current|now|ongoing|till date|to date|today"
_RANGE = re.compile(rf"(?i)(?P<s>{_TOKEN})\s*(?:-|–|—|to|until|through|thru)\s*(?P<e>{_TOKEN}|{_PRESENT})(?![\w])")
# Some generators (AwesomeCV) draw the dash as a vector, so the text layer holds "Sep. 2023 Mar. 2024"; two adjacent
# month-precision tokens with nothing between them read as a range.
_MONTH_TOKEN = rf"(?:{_MON}\.?\s*(?:['’]\d{{2}}|\d{{4}})|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{1,2}})"
_PAIR = re.compile(rf"(?i)(?<![\w])(?P<s>{_MONTH_TOKEN})\s+(?P<e>{_MONTH_TOKEN}|{_PRESENT})(?![\w])")
_SINGLE = re.compile(rf"(?i)(?<![\w])(?P<s>{_TOKEN})(?![\w])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_DP_SETTINGS = {"PREFER_DAY_OF_MONTH": "first", "PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False}


def redact(value: str) -> str:
    """Contact values never reach a log in clear text."""
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    return f"***{digits[-2:]}" if len(digits) >= 4 else "***"


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def _norm_heading(text: str) -> str:
    t = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", text.strip().lower()).replace("&", " and ")
    return " ".join(re.sub(r"[^a-z ]", " ", t).split())


def heading_kind(text: str) -> str | None:
    return _PHRASE_TO_KIND.get(_norm_heading(text))


def detect(ex, n_pages: int) -> tuple[float, list[str]]:
    """(confidence, canonical headings found). confidence = 0.5*min(canonical/3,1) + 0.25*(<=2 pages) + 0.25*(no References).
    The layer runs when at least 3 canonical headings are present; the other two criteria lower the confidence."""
    found = []
    for h in ex.block.headings:
        k = heading_kind(h.text)
        if k and k not in found:
            found.append(k)
    for u in ex.units:  # single-font documents: a short standalone line that IS a canonical heading
        if u.kind == "paragraph" and len(u.text.split()) <= 4:
            k = heading_kind(u.text)
            if k and k not in found:
                found.append(k)
    has_refs = ex.block.references.section_found
    conf = 0.5 * min(len(found) / 3, 1.0) + 0.25 * (n_pages <= 2) + 0.25 * (not has_refs)
    return round(conf, 3), found


# --------------------------------------------------------------------------
# contact
# --------------------------------------------------------------------------


def contact_block(ex, extra_emails: list[str] | None = None) -> ContactBlock:
    head = ex.full_text[:4000]
    emails = sorted({m.group(0).lower() for m in _EMAIL.finditer(head)} | {e.lower() for e in extra_emails or []})
    phones: list[str] = []
    for m in phonenumbers.PhoneNumberMatcher(head, None, leniency=phonenumbers.Leniency.VALID):
        e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in phones:
            phones.append(e164)
    urls = [l.url for l in ex.block.links if not l.url.lower().startswith("mailto:")]
    log.debug("contact block: %d emails, %d phones, %d urls", len(emails), len(phones), len(urls))
    return ContactBlock(emails=emails, phones=phones, urls=urls)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------


def _last_day(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_token(tok: str, today: date) -> tuple[date, date, str] | None:
    """(earliest, latest, precision) the token can mean, or None. Never guesses."""
    t = tok.strip()
    low = t.lower().rstrip(".")
    if re.fullmatch(_PRESENT, low):
        return today, today, "day"
    m = re.fullmatch(r"(?i)(?:spring|summer|fall|autumn|winter)\s+(\d{4})", t)
    if m:
        y, mo = int(m.group(1)), _SEASONS[low.split()[0]]
        end_m = mo + 2
        return date(y, mo, 1), _last_day(y + (end_m > 12), end_m - 12 if end_m > 12 else end_m), "season"
    if re.fullmatch(r"(19|20)\d{2}", t):
        y = int(t)
        return date(y, 1, 1), date(y, 12, 31), "year"
    m = re.fullmatch(r"(?i)(" + _MON + r")\.?\s*['’](\d{2})", t)
    if m:  # Jun'22: apostrophe year, handled here because dateparser reads it as DAY 22
        mo, yy = _MONTHS[m.group(1).lower()], int(m.group(2))
        y = 2000 + yy if yy <= 69 else 1900 + yy
        return date(y, mo, 1), _last_day(y, mo), "month"
    m = re.fullmatch(r"(\d{1,2})/(\d{4})|(\d{4})-(\d{1,2})", t)
    if m:
        mo, y = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        if not 1 <= mo <= 12:  # dateparser would reinterpret 13/2020 as 13 October
            return None
        ok = dateparser.parse(t, settings=_DP_SETTINGS, languages=["en"])
        if ok and (ok.year, ok.month) == (y, mo):
            return date(y, mo, 1), _last_day(y, mo), "month"
        return None
    m = re.fullmatch(r"(?i)(" + _MON + r")\.?\s*(\d{4})", t)
    if m:
        mo, y = _MONTHS[m.group(1).lower()], int(m.group(2))
        ok = dateparser.parse(f"{m.group(1)} {y}", settings=_DP_SETTINGS, languages=["en"])
        if ok and (ok.year, ok.month) == (y, mo):
            return date(y, mo, 1), _last_day(y, mo), "month"
    return None


def _entry(raw: str, s_tok: str, e_tok: str | None, section: str | None, context: str, today: date) -> TimelineEntry:
    s = parse_token(s_tok, today)
    is_cur = bool(e_tok and re.fullmatch(_PRESENT, e_tok.strip().lower()))
    e = parse_token(e_tok, today) if e_tok else s
    if s is None or e is None:
        return TimelineEntry(raw=raw, is_current=is_cur, section=section, context=context[:200], confidence=0.2)
    prec = {"day": 1.0, "month": 1.0, "season": 0.6, "year": 0.7}
    conf = round(min(prec[s[2]], prec[e[2]]), 2)
    return TimelineEntry(raw=raw, start=s[0].isoformat(), end=e[1].isoformat(), start_precision=s[2], end_precision=e[2],
                         is_current=is_cur, section=section, context=context[:200], confidence=conf)


def build_timeline(ex, today: date) -> list[TimelineEntry]:
    out: list[TimelineEntry] = []
    sections = ex.block.sections
    seen: set[tuple[str, str]] = set()
    for u in ex.units:
        sec = sections[u.section_index].heading if 0 <= u.section_index < len(sections) else None
        kind = heading_kind(sec) if sec else None
        for ln in u.lines:
            text = ln.text
            ranges = list(_RANGE.finditer(text))
            ranges += [m for m in _PAIR.finditer(text) if not any(a.start() <= m.start() < a.end() for a in ranges)]
            spans = [(m.start(), m.end()) for m in ranges]
            for m in ranges:
                key = (m.group(0), text[:80])
                if key not in seen:
                    seen.add(key)
                    out.append(_entry(m.group(0), m.group("s"), m.group("e"), sec, text, today))
            if kind in ("experience", "education", "projects", "certifications", "publications") and len(text) <= 160:
                for m in _SINGLE.finditer(text):
                    if not any(a <= m.start() < b for a, b in spans):
                        key = (m.group(0), text[:80])
                        if key not in seen:
                            seen.add(key)
                            out.append(_entry(m.group(0), m.group("s"), None, sec, text, today))
    return out


# --------------------------------------------------------------------------
# contradictions
# --------------------------------------------------------------------------


def _d(iso: str | None) -> date | None:
    return date.fromisoformat(iso) if iso else None


def _bounds(e: TimelineEntry) -> tuple[date, date, date, date] | None:
    """(start_min, start_max, end_min, end_max): where the start and end can each lie given their precision."""
    s, en = _d(e.start), _d(e.end)
    if not s or not en:
        return None

    def last(d: date, prec: str) -> date:
        return {"year": date(d.year, 12, 31), "month": _last_day(d.year, d.month), "season": _last_day(d.year + (d.month + 2 > 12), (d.month + 2 - 1) % 12 + 1),
                "day": d}[prec]

    def first(d: date, prec: str) -> date:
        return {"year": date(d.year, 1, 1), "month": date(d.year, d.month, 1), "season": date(d.year, d.month, 1), "day": d}[prec]

    return s, last(s, e.start_precision or "day"), first(en, e.end_precision or "day"), en


def _is_experience(e: TimelineEntry) -> bool:
    return bool(e.section) and heading_kind(e.section) == "experience"


def _full_time(e: TimelineEntry) -> bool:
    return not PART_TIME.search(e.context)


def contradictions(entries: list[TimelineEntry], full_text: str, today: date) -> list[Contradiction]:
    out: list[Contradiction] = []
    for e in entries:  # a range that ends before it starts
        b = _bounds(e)
        if b and e.start and b[3] < b[0] and e.raw != e.start:
            out.append(Contradiction(kind="end_before_start", detail="the end date is earlier than the start date", cites=[e.raw]))
    ok = [e for e in entries if _is_experience(e) and _full_time(e) and (b := _bounds(e)) and b[3] >= b[0]]
    for i, a in enumerate(ok):
        for c in ok[i + 1:]:
            ba, bc = _bounds(a), _bounds(c)
            if a.is_current and c.is_current:
                continue  # reported once, as multiple_present_roles
            lo, hi = max(ba[1], bc[1]), min(ba[2], bc[2])  # the span each role CERTAINLY covers
            if (hi - lo).days >= OVERLAP_MIN_DAYS:
                out.append(Contradiction(kind="overlapping_full_time_roles", cites=[a.raw, c.raw],
                                         detail=f"two full-time roles overlap by {(hi - lo).days} days"))
    current = [e for e in entries if _is_experience(e) and _full_time(e) and e.is_current]
    if len(current) >= 2:
        out.append(Contradiction(kind="multiple_present_roles", cites=[e.raw for e in current[:4]],
                                 detail=f"{len(current)} full-time roles are listed as current"))
    edu = [e for e in entries if e.section and heading_kind(e.section) == "education" and _bounds(e)]
    for e in entries:
        if not _is_experience(e) or not _bounds(e):
            continue
        title = e.context.split(e.raw)[0]
        for cred, pat in CREDENTIALS.items():
            if re.search(pat, title, re.I):
                for ed in edu:
                    if re.search(pat, ed.context, re.I) and _bounds(e)[1] < _bounds(ed)[2]:
                        out.append(Contradiction(kind="credential_before_education", cites=[e.raw, ed.raw],
                                                 detail=f"a role requiring a {cred.upper()} starts before that degree could have been completed"))
    claims = [(int(m.group(1)), m.group(0)) for m in CLAIMED_YEARS.finditer(full_text) if int(m.group(1)) <= 60]
    pool = [e for e in entries if _is_experience(e) and e.start] or [e for e in entries if e.start]
    if claims and pool:
        earliest = min(pool, key=lambda e: e.start)
        span_years = (today - _d(earliest.start)).days / 365.25
        for years, raw in claims:
            if years > span_years + CONTRADICTION_TOLERANCE_YEARS:
                out.append(Contradiction(kind="experience_exceeds_span", cites=[raw, earliest.raw],
                                         detail=f"claims {years} years of experience; the earliest dated entry is {span_years:.1f} years ago"))
    return out


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------


def load_lexicon(path: Path = LEXICON_PATH) -> tuple[dict[str, tuple[str, bool]], list[str]]:
    """({normalised alias: (canonical, ambiguous)}, aliases eligible for fuzzy matching)."""
    table: dict[str, tuple[str, bool]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ambiguous = line.startswith("!")
        names = [n.strip() for n in line.lstrip("!").split("|") if n.strip()]
        for n in names:
            table[_norm_term(n)] = (names[0], ambiguous)
    fuzzy = [a for a, (_c, amb) in table.items() if len(a) >= config.SKILL_FUZZY_MIN_LEN and not amb]
    return table, fuzzy


def _norm_term(s: str) -> str:
    return re.sub(r"[^a-z0-9+#./ ]", "", s.lower()).strip(" .")


_SPLIT = re.compile(r"[,;|•·●▪/\n()\[\]:]+|\s+(?:and|&|or)\s+|\s{2,}")


def _candidates(text: str):
    """Phrases (split on list delimiters) and 1-3 word n-grams inside them, with the text as written."""
    for phrase in _SPLIT.split(text):
        words = phrase.split()
        for i in range(len(words)):
            for n in (1, 2, 3):
                if i + n <= len(words):
                    yield " ".join(words[i:i + n])


def skills_mentioned(ex) -> list[SkillMention]:
    table, fuzzy = load_lexicon()
    sections = ex.block.sections
    found: dict[str, SkillMention] = {}
    for u in ex.units:
        if u.kind != "paragraph":
            continue
        sec = sections[u.section_index].heading if 0 <= u.section_index < len(sections) else None
        if sec and re.search(r"(?i)^(?:\d+(?:\.\d+)*\.?\s+)?references|bibliography", sec.strip()):
            continue
        skills_like = bool(sec and SKILLS_LIKE.search(sec))
        hits: list[tuple[str, str, bool, float]] = []
        for cand in _candidates(u.text):
            key = _norm_term(cand)
            if key in table:
                canon, amb = table[key]
                hits.append((canon, cand, True, 100.0) if not amb else (canon, cand, True, -1.0))
            elif len(key) >= config.SKILL_FUZZY_MIN_LEN and " " not in key.strip() and fuzzy:
                best = process.extractOne(key, fuzzy, scorer=fuzz.ratio, score_cutoff=config.SKILL_FUZZY_CUTOFF)
                if best:
                    hits.append((table[best[0]][0], cand, False, float(best[1])))
        strong = sum(1 for h in hits if h[3] != -1.0)
        for canon, cand, exact, score in hits:
            if score == -1.0:  # ambiguous term: only in a skills-like context
                if not (skills_like or strong >= 3):
                    continue
                score = 100.0
            if canon not in found:
                found[canon] = SkillMention(skill=canon, matched=cand, exact=exact, score=score, section=sec)
    return sorted(found.values(), key=lambda s: s.skill.lower())


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_resume(ex, n_pages: int, today: date | None = None, extra_emails: list[str] | None = None) -> tuple[float, ResumeBlock | None]:
    """(resume_confidence, ResumeBlock or None when fewer than 3 canonical headings are present)."""
    today = today or date.today()
    confidence, found = detect(ex, n_pages)
    if len(found) < 3:
        return confidence, None
    entries = build_timeline(ex, today)
    return confidence, ResumeBlock(contact_block=contact_block(ex, extra_emails), timeline=entries,
                                   timeline_contradictions=contradictions(entries, ex.full_text, today),
                                   skills_mentioned=skills_mentioned(ex))


_ = datetime  # (kept for callers that pass datetimes)
