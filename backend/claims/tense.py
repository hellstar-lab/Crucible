"""The tense guard: Stage 4 must never turn a modality-bearing construction of its source into done work, and never
invert its source's polarity. Deterministic, NO VERB LEXICON (Stage 3 item 3).

Why narrowed (counted over five live runs on the OSDBMS phase-II report, docs/claim-track-osdbms-phase2.md): every
correct catch was a purpose or modality construction stated as an outcome ("to regulate" -> "Regulated", "to avoid" ->
"avoided", "Preventing ..." -> "Prevented"); every false positive and most misses came from comparing all verb forms
through a verb list (a missing verb, a noun read as the main verb, a participle under an unlisted main verb).

tense_changed      The SOURCE has an explicit modality-bearing construction, and the CLAIM states its verb in a finite
                   form (past -ed, present -s, or a bare present form after a subject):
                     to_verb             "to + VERB" or "to + VERBing": an infinitive of purpose, "prior to exploring"
                     verbing_fragment    the source is a bare VERBing phrase with no finite verb ("Preventing Race ...")
                     passive_participle  "VERBed by", in a source with no finite verb ("a buffer managed by the OS
                                         layer" as a fragment); a present participle is NOT a construction
                     will_plan           "will / shall / plan(s|ned) to / intend(s) to / aim(s) to / going to + VERB"
                   Verbs are matched by a suffix stem (regulate ~ regulated, explore ~ explores), never by a list. A
                   claim word that also occurs in the source OUTSIDE a construction is the source's own word, and passes.
negation_inverted  The PARITY of negations differs between the claim and the source clause it came from. A source
                   negation counts only when a content word in its scope appears in the claim; a un-/non- word counts as
                   a negation when the other side has the root. Negators: not, n't, never, no, without, lacks, fails to,
                   absent, rather than, instead of ("before" and "until" are not negators).
Claims whose source has no such construction are not_applicable: the guard has nothing to judge, and says so.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*")
NEG_WORDS = frozenset("not never no without lacks lack lacked lacking absent".split())
NEG_PHRASES = (r"\bfail(?:s|ed|ing)? to\b", r"\brather than\b", r"\binstead of\b")
FUNCTION = frozenset("""a an the this that these those our their its his her my your to of in on at for with by from into via
and or but as is are was were be been being has have had do does did will would shall should can could may might must not
no it they we i he she them us there here than then so such also very more most less only just""".split())
_DETERMINERS = frozenset("a an the our their its his her my your".split())  # "the processed data": an adjective
_AUX = frozenset("is are was were be been being am has have had do does did will would shall should can could may might "
                 "must isn't aren't wasn't weren't hasn't haven't didn't doesn't won't can't".split())
# -ing words that open a phrase as an ADJECTIVE, not a gerund ("Incoming portal requests route properly", measured)
ING_ADJECTIVES = frozenset("""incoming outgoing upcoming ongoing existing remaining following leading interesting underlying
corresponding resulting missing pending growing rising living willing outstanding""".split())
_WILL_PLAN = re.compile(r"(?i)\b(?:will|shall|(?:plan|plans|planned|intend|intends|aim|aims|going|hope|hopes)\s+to)\s+(?:be\s+)?([a-z]+)")
_TO_VERB = re.compile(r"(?i)\bto\s+(?:be\s+)?([a-z]{3,})")


def stem(word: str) -> str:
    """A suffix stem, no lexicon: regulate / regulated / regulating -> regulat; planned -> plan; explores -> explor."""
    w = word.lower().strip("'")
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 2 and not w.endswith("ss"):
            w = w[: -len(suf)]
            break
    if w.endswith("e") and len(w) > 2:
        w = w[:-1]  # use / used / uses -> us; regulate / regulated -> regulat
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]  # planned -> plann -> plan (but "fill", "pass" keep their double letter)
    return w


def _words(text: str) -> list[str]:
    return _TOKEN.findall(re.sub(r"^\s*(?:\d{1,2}[.)]|[-*•])\s*", "", text or ""))


def _has_finite(words: list[str]) -> bool:
    """A finite verb the source states without a lexicon: an auxiliary / modal, or a past form (-ed) that is not a
    passive participle ("managed by") and not an adjective after a determiner."""
    for i, w in enumerate(words):
        low = w.lower()
        if low in _AUX:
            return True
        if (low.endswith("ed") and len(low) > 4 and "-" not in w and (not i or words[i - 1].lower() not in _DETERMINERS)
                and not (i + 1 < len(words) and words[i + 1].lower() == "by")):
            return True
    return False


def verbing_fragment(text: str) -> str | None:
    """The opening VERBing word when the text is a bare gerund phrase with no subject and no finite verb, else None."""
    if "," in (text or ""):
        return None  # "Moving forward, efforts center on ...": an introductory phrase, then a clause
    words = _words(text)
    if not words or not words[0].lower().endswith("ing") or len(words[0]) <= 4 or words[0].lower() in ING_ADJECTIVES:
        return None
    return None if _has_finite(words[1:]) else words[0]


def constructions(source: str) -> list[tuple[str, str, str]]:
    """[(stem, construction, word as written)] for every modality-bearing construction of the source."""
    out = []
    for m in _WILL_PLAN.finditer(source or ""):
        if m.group(1).lower() not in FUNCTION:
            out.append((stem(m.group(1)), "will_plan", m.group(0)))
    for m in _TO_VERB.finditer(source or ""):
        w = m.group(1)
        if w.lower() not in FUNCTION and not any(c[2].endswith(w) for c in out):
            out.append((stem(w), "to_verb", m.group(0)))
    frag = verbing_fragment(source)
    if frag:
        out.append((stem(frag), "verbing_fragment", frag))
    words = _words(source)
    if not _has_finite(words):
        for i, w in enumerate(words):
            nxt = words[i + 1].lower() if i + 1 < len(words) else ""
            if w.lower().endswith("ed") and nxt == "by" and "-" not in w:
                out.append((stem(w), "passive_participle", w))
    # NOT present participles ("a pipeline separating X", "surges ..., requiring Y"): without a verb list the source's
    # own finite verb ("utilizes", "slam") is invisible, so "separating" -> "separates" read as a change. Measured on
    # the five recorded runs: 7 false positives, all from present participles, and no catch.
    return out


def _finite_in_claim(words: list[str], i: int) -> bool:
    """Is claim word i a finite form? -ed (not a modifier after a determiner), -s after a word, or a bare form after a
    non-function word (a subject). Not after "to" or a modal; never an -ing form."""
    w, low = words[i], words[i].lower()
    prev = words[i - 1].lower() if i else ""
    if low.endswith("ing") or prev in ("to",) or prev in _AUX - {"is", "are", "was", "were", "has", "have", "had"}:
        return False
    if low.endswith("ed"):
        return prev not in ("a", "an", "the")
    if low.endswith("s") and not low.endswith("ss"):
        return i > 0
    return i > 0 and prev not in FUNCTION  # "The tuned thresholds avoid bottlenecks": a bare present after a subject


def tense_changed(source: str, claim: str) -> str | None:
    cons = constructions(source)
    if not cons:
        return None
    src_free = {w.lower() for w in _TOKEN.findall(source or "")}
    in_cons = {c[2].split()[-1].lower() for c in cons}
    words = _TOKEN.findall(claim or "")
    for i, w in enumerate(words):
        if "-" in w or len(w) < 3:
            continue
        st = stem(w)
        hit = next((c for c in cons if c[0] == st), None)
        if hit is None or not _finite_in_claim(words, i):
            continue
        if w.lower() in src_free and w.lower() not in in_cons:
            continue  # the source itself states this form, outside the construction
        return f"{hit[1]}: the source has {hit[2]!r}; the claim states {w!r} as done or true"
    return None


# ---- negation (polarity), unchanged in behaviour, now lexicon-free -------------------------------------------------
SCOPE_WORDS = 3
_STOP = frozenset("the a an and or of to in on for with by from at as is are was were be been this that these those our "
                  "their its it we they under over into onto about after before until within across through via per than "
                  "then".split())


def _unprefixed(t: str) -> str:
    for pre in ("un", "non"):
        if t.startswith(pre) and len(t) > len(pre) + 3:
            return t[len(pre):].lstrip("-")
    return t


def _keys(word: str) -> set[str]:
    w = _unprefixed(word.lower())
    return {w, stem(w)}


def _negators_with_scope(text: str) -> list[tuple[str, list[str]]]:
    low = (text or "").lower()
    spans = [(m.start(), m.end(), m.group(0)) for p in NEG_PHRASES for m in re.finditer(p, low)]
    spans += [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[a-z][a-z'-]*", low)
              if m.group(0) in NEG_WORDS or m.group(0).endswith("n't")]
    out = []
    for _a, b, neg in sorted(spans):
        rest = re.split(r"[,.;:!?]", low[b:], maxsplit=1)[0]
        words = [w for w in _TOKEN.findall(rest) if len(w) > 2 and w not in _STOP and w not in NEG_WORDS]
        out.append((neg, words[:SCOPE_WORDS]))
    return out


def _negations(text: str, other: str, scoped: bool = False) -> int:
    other_keys = set().union(*(_keys(w) for w in _TOKEN.findall((other or "").lower()))) if other else set()
    n = sum(1 for _neg, scope in _negators_with_scope(text) if not scoped or any(_keys(w) & other_keys for w in scope))
    other_words = set(_TOKEN.findall((other or "").lower()))
    for t in _TOKEN.findall((text or "").lower()):
        for pre in ("un", "non"):
            if t.startswith(pre) and len(t) > len(pre) + 3 and t[len(pre):].lstrip("-") in other_words:
                n += 1
    return n


_CLAUSE = re.compile(r"[.;:!?]+|,\s*(?=(?:and|but|while|whereas|so|where|which|who|whose)\b)")


def _content(text: str) -> set[str]:
    return {stem(_unprefixed(t)) for t in _TOKEN.findall((text or "").lower()) if len(t) > 2 and t not in _STOP and t not in NEG_WORDS}


def _clause_for(source: str, claim: str) -> str | None:
    want = _content(claim)
    best, score = None, 1
    for cl in _CLAUSE.split(source or ""):
        n = len(want & _content(cl))
        if n > score:
            best, score = cl, n
    return best


def negation_inverted(source: str, claim: str) -> str | None:
    clause = _clause_for(source, claim)
    if clause is not None:
        c, s = _negations(claim, clause), _negations(clause, claim, scoped=True)
        if s % 2 != c % 2:
            return f"negation parity differs: {c} in the claim, {s} in the source clause {clause.strip()[:80]!r}"
        return None
    c, s = _negations(claim, source), _negations(source, claim, scoped=True)
    if c % 2 == 1 and s == 0:
        return f"the claim negates ({c}) where the source has no negation"
    return None


def check(source: str, claim: str) -> tuple[str, str] | None:
    d = negation_inverted(source, claim)
    if d:
        return "negation_inverted", d
    d = tense_changed(source, claim)
    if d:
        return "tense_changed", d
    return None


def judge(source: str, claim: str) -> tuple[str, str]:
    """("rejected", (code, detail)) | ("passed", "") when the source has a construction the claim kept |
    ("not_applicable", why) when the source has no modality-bearing construction to compare."""
    hit = check(source, claim)
    if hit:
        return "rejected", hit  # type: ignore[return-value]
    if constructions(source):
        return "passed", ""
    return "not_applicable", "the source has no modality-bearing construction"
