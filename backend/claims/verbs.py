"""Does a piece of resume text contain an action verb? Deterministic, lexicon-based, no model, no new dependency.

Used for two things (Phase 4M):
  * M1: a verbless entry line under Projects / Experience / Education is a reference, not a claim;
  * M2: the verbless-source guard. If the SOURCE unit has no verb, a claim derived from it that HAS one was given an
    assertion the resume never made ("Crucible, hiring-evidence system" -> "Built a Crucible, hiring-evidence system",
    measured, and it came out supported as ownership).

Deliberately narrow. This is NOT a general verb-match between claim and source: a full match breaks paraphrase, which
is why claim_support_ratio was abandoned (a real paraphrase measured 0.707, below both fabrications). It only asks
"verb present at all?", with stemming, so "redesigning" -> "Redesigned" is the same verb.

Source and claim are read slightly differently, both on the cautious side:
  * in a SOURCE, an -ing word is a verb only when it starts the unit or takes an object ("redesigning the API"), so
    "Redis caching layer" and "hiring-evidence system" stay verbless; a hyphenated compound is never a verb;
  * in a CLAIM, any lexicon form counts ("Built", "building", "builds");
  * in both, a noun-like base form mid-phrase ("Senior Engineer", "release pipeline") is a noun.
A noun-like base form that STARTS a source unit ("Release pipeline for mobile apps", "Design the API") is
verb_ambiguous and counts as VERBLESS: without a POS tagger the guard cannot tell a noun phrase from an imperative,
so it errs to the conservative side. A real claim from such a unit is then excluded ("could not process") rather than
a fabricated verb passing.
"""

from __future__ import annotations

import re

# resume action verbs and the common general ones, base forms; irregular past forms listed separately
_BASE = """
accelerate achieve add administer advise analyse analyze architect assemble audit author automate balance benchmark
build calibrate championed change clean code collaborate compile complete compose configure consolidate construct
contribute convert coordinate create cut debug decrease define deliver deploy design detect develop devise diagnose
direct document drive earn eliminate enable engineer enhance establish evaluate expand experiment extend extract
facilitate fix found generate grow guide handle harden head help hire identify implement improve increase instrument
integrate introduce invent investigate launch lead lower maintain manage map measure mentor merge migrate model
modernize monitor move negotiate optimise optimize orchestrate organize own oversee package pair parallelize partner
patch pilot plan port prioritize process produce profile program prototype provision publish rebuild
recruit redesign reduce refactor release rename replace research resolve restructure retire review rewrite run save
scale schedule secure serve set ship simplify solve spearhead speed standardize start streamline strengthen
supervise support teach test train transform translate triage troubleshoot tune unify update upgrade use validate
verify win work write
""".split()
_IRREGULAR = """built led wrote written ran grew drove won made took taught began begun found founded rebuilt rewrote
rewritten shipped cut set spent sped held hired kept got gave given oversaw overseen""".split()
_AUX = "is was were are been has had have did does".split()  # a sentence the resume did not contain


_BASE_SET = frozenset(_BASE)
_FORMS = frozenset(_IRREGULAR) | frozenset(_AUX)
# base forms that are just as often nouns ("release pipeline", "test suite"): in a SOURCE they count as a verb only as
# the first word ("Design the API"), never mid-phrase
_NOUNISH = frozenset("""release test model map package profile program process support plan change set work use run cut head
lead design build code document fix patch pilot review schedule secure serve ship start train update upgrade win partner
pair port handle help research experiment balance benchmark monitor debug audit engineer""".split())
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*")
_OBJECT_STARTERS = frozenset("the a an our their its his her my this that these those to with for from into on in across "
                             "of and all new".split())


def lemmas(word: str) -> set[str]:
    """Candidate base forms: redesigning -> redesign, used -> use, planned -> plan, optimized -> optimize, studies -> study."""
    w = word.lower().strip(".,;:()[]\"'")
    out = {w}
    for suf in ("ing", "ed", "es", "s", "d"):
        if w.endswith(suf) and len(w) > len(suf) + 1:
            root = w[: -len(suf)]
            out |= {root, root + "e"}
            if len(root) > 2 and root[-1] == root[-2]:
                out.add(root[:-1])  # planned -> plann -> plan
            if root.endswith("i"):
                out.add(root[:-1] + "y")  # studied -> studi -> study
    return out


def _verb_form(tok: str) -> str | None:
    """The base form when ``tok`` is a verb form, else None."""
    low = tok.lower()
    if low in _FORMS:
        return low
    hit = lemmas(low) & _BASE_SET
    return next(iter(hit)) if hit else None


def stem(word: str) -> str:
    """The verb's base form if it is one ("Redesigned" -> "redesign"), else the lower-cased word."""
    return _verb_form(word) or word.lower()


def has_verb(text: str, *, source: bool = False) -> bool:
    toks = _TOKEN.findall(text or "")
    for i, tok in enumerate(toks):
        if "-" in tok:
            continue  # hyphenated compound ("hiring-evidence"): a modifier, not a verb
        base = _verb_form(tok)
        if base is None:
            continue
        low = tok.lower()
        if low == base and base in _NOUNISH and (i != 0 or source):
            # mid-phrase: a noun ("Senior Engineer"). Unit-initial in a SOURCE: verb_ambiguous ("Release pipeline ..."
            # or "Design the API"?), treated as VERBLESS so the guard errs to "could not process", never to a
            # fabricated assertion (Phase 4G fold-in from the M2 review)
            continue
        if source and low.endswith("ing"):
            nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
            if not (i == 0 or nxt in _OBJECT_STARTERS):
                continue  # "Redis caching layer": a noun modifier
        return True
    return False


def fabricated_assertion(source: str, claim: str) -> bool:
    """M2: the source states no verb, the claim does."""
    return not has_verb(source, source=True) and has_verb(claim)


def verb_ambiguous(text: str) -> bool:
    """The unit's first word is verb-shaped but could be a noun (a bare noun-like base form)."""
    toks = _TOKEN.findall(text or "")
    if not toks or "-" in toks[0]:
        return False
    base = _verb_form(toks[0])
    return base is not None and toks[0].lower() == base and base in _NOUNISH
