"""Is a claim derivable from its own source text? Deterministic, no model.

Why (Phase 4.0c, G6): asked to open a URL, no tier refused and all three returned an INVENTED fetch timestamp:
a plausible fabrication filling a required field. The refusal detector (llm.detect_refusal) cannot catch that
and must not try. What can be checked offline is whether a claim's content is in the text it came from.

A similarity THRESHOLD does not work and was removed. Measured: rapidfuzz token-set overlap ranked both
fabrications (invented number 0.958, absent technology 0.898) ABOVE a legitimate paraphrase (0.707). What
separates them is the TYPE of the unmatched token:

  fabricated_quantity  a number or quantity in the claim absent from the source (12k / 12,000 / 12000 are equal).
                       HARD FAIL.
  fabricated_entity    a technology (2C tech lexicon) or proper noun in the claim absent from the source. HARD FAIL.
  ignored              any other unmatched word. That is paraphrase, not fabrication.

Words are compared after light stemming (cache / caching / cached match). ``ratio`` (token-set overlap) is kept as
a reported diagnostic only and gates nothing.

``source`` is the unit's span text plus the context the claim was allowed to draw on (its heading path and
neighbouring units): Stage 3 legitimately moves a referent from the context into a claim.

Known limits: spelled-out numbers ("twelve") are not quantities here; a proper noun that is the claim's first
word is only recognised when it is in the lexicon or not a plain capitalised word (e.g. "PostgreSQL", "AWS").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from rapidfuzz import fuzz

STOPWORDS = frozenset("""
a an and are as at be been being by for from has have had he her his i in into is it its of on or our she so
that the their them they this those to was we were which while who will with you your over per via than then
also both each such about after before during under within across between using used use
""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]|[A-Za-z]")
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_QUANTITY = re.compile(r"(?<![\w.])[$€£]?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(k|thousand|mm|m|million|bn|b|billion)?(?![a-zA-Z])",
                       re.IGNORECASE)
_SUFFIXES = ("ations", "ation", "ingly", "edly", "ings", "ing", "ers", "er", "ies", "ied", "es", "ed", "s", "e", "y")


def stem(word: str) -> str:
    w = word.casefold()
    for suf in _SUFFIXES:
        if len(w) - len(suf) >= 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def quantities(text: str) -> list[tuple[str, float]]:
    """(as written, normalised value) for every number in the text. 12k, 12,000 and 12000 all give 12000.0."""
    out = []
    for m in _QUANTITY.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        if m.group(2):
            value *= _SCALE[m.group(2).lower()]
        out.append((m.group(0).strip(), round(value, 6)))
    return out


@lru_cache(maxsize=1)
def _lexicon():
    from backend.shield.resume import _norm_term, load_lexicon  # read-only use of the 2C lexicon

    table, _fuzzy = load_lexicon()
    return table, _norm_term


def _ngrams(words: list[str], n_max: int = 3):
    for i in range(len(words)):
        for n in range(n_max, 0, -1):
            if i + n <= len(words):
                yield i, n, " ".join(words[i:i + n])


# Calendar words are dates, not entities ("Feb" in the source, "February" in a claim is not a fabrication).
_CALENDAR = frozenset("""
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
monday tuesday wednesday thursday friday saturday sunday present
""".split())


def entities(text: str) -> list[str]:
    """Technologies (lexicon canonical names) and proper nouns, as written."""
    table, norm = _lexicon()
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#./-]*", (text or "").replace("-", " "))
    # sentence punctuation is not part of a word: "Pass." in a claim is "Pass" (measured: "Status (Pass/Fail): Pass."
    # failed against the source's "Pass;" as a fabricated entity 'Pass.'). Internal dots stay ("Node.js", "ASP.NET").
    raw = [w.rstrip("./") for w in raw if w.rstrip("./")]
    out, used = [], set()
    for i, n, phrase in _ngrams(raw):
        if any(j in used for j in range(i, i + n)):
            continue
        hit = table.get(norm(phrase))
        if hit:
            canon, ambiguous = hit
            if ambiguous and not phrase[:1].isupper():  # "go", "rust" as plain words are not technologies
                continue
            out.append(canon)
            used.update(range(i, i + n))
    for i, w in enumerate(raw):
        if i in used or not w[:1].isupper() or w.casefold() in STOPWORDS or w[:1].isdigit() or w.casefold().rstrip(".") in _CALENDAR:
            continue
        plain_capital = w[1:].islower()
        if i == 0 and plain_capital:  # sentence-initial capital: not evidence of a proper noun
            continue
        out.append(w)
    return out


def _content_stems(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text or "") if w.casefold() not in STOPWORDS]


@dataclass
class SupportCheck:
    ratio: float  # diagnostic only: token-set overlap of stemmed content words, 0..1
    fabricated_quantities: list[str] = field(default_factory=list)
    fabricated_entities: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # unmatched common words: paraphrase

    @property
    def hard_fail(self) -> bool:
        return bool(self.fabricated_quantities or self.fabricated_entities)


def claim_support_ratio(claim: str, source: str) -> float:
    c, s = _content_stems(claim), _content_stems(source)
    return round(fuzz.token_set_ratio(" ".join(c), " ".join(s)) / 100, 4) if c else 0.0


def check(claim: str, source: str) -> SupportCheck:
    """Typed comparison of a claim against its source (span text + allowed context)."""
    src_values = {v for _w, v in quantities(source)}
    fq = [w for w, v in quantities(claim) if v not in src_values]

    table, norm = _lexicon()
    src_stems = set(_content_stems(source))
    src_entities = {e.casefold() for e in entities(source)}
    fe = []
    for e in entities(claim):
        key = e.casefold()
        if key in src_entities or stem(e) in src_stems or all(stem(p) in src_stems for p in e.split()):
            continue
        fe.append(e)

    entity_stems = {stem(p) for e in entities(claim) for p in e.split()}
    quantity_words = {w.casefold() for w, _v in quantities(claim)}
    ignored = [w for w in _WORD.findall(claim) if w.casefold() not in STOPWORDS and stem(w) not in src_stems
               and stem(w) not in entity_stems and w.casefold() not in quantity_words]
    return SupportCheck(ratio=claim_support_ratio(claim, source), fabricated_quantities=fq, fabricated_entities=fe, ignored=ignored)
