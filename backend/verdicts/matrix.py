"""The verdict matrix. Deterministic. The model judges SEMANTICS only; code decides AUTHORSHIP. Never the reverse.

Why: the difference between "this code exists in a repo they pushed" and "they wrote this code" IS the product. A
model that could get that wrong could invalidate the whole project, so it is decided here from blame data (4B's
authored_by_candidate on the CITED evidence item), and the model never sees the authorship fields.

  content_supported | authored_by_candidate | identity reliable | FINAL
  supported         | True                  | yes               | supported
  supported         | False                 | yes               | OWNERSHIP RULE
  supported         | False                 | no (identity gap) | inconclusive
  supported         | None                  | -                 | inconclusive
  contradicted      | any                   | -                 | contradicted
  no_evidence       | -                     | -                 | no_evidence
  inconclusive      | -                     | -                 | inconclusive

OWNERSHIP RULE (the evidence exists, someone else wrote it): Stage 5's claim kind decides. ``ownership`` (the claim
asserts "Built X" / "I wrote X") -> contradicted: the evidence actively disagrees with the ownership assertion. Any
other kind (the claim asserts existence or use) -> supported, with an authorship note saying the candidate did not
author it.

SCOPE RULE (Phase 4C F1, must always hold): a contradiction requires the evidence and the claim to be about the SAME
OBJECT. Crucible sees a partial sample of repositories (public only, candidate-selected, unknown total), so absence in
that sample is never evidence of absence, and a career, timeline or aggregate claim can NEVER be contradicted by a
repository sample. "contradicted" is permitted ONLY when the claim's scope is specific_artifact or project AND the cited
evidence item belongs to that same artifact. Otherwise the verdict is inconclusive: "evidence is not about the same scope
as the claim". Measured failure that motivated it: "2014 - 2018" (a degree's dates) came back contradicted in 3 of 3 runs
because one repository's commit history spans one day in 2026; Ultra's reasoning was locally valid and globally wrong.
The history/count check below is a special case, kept as an explicit early check for clarity.

HARD GATES, in code:
  * retrieval_performed False      -> unverifiable, 4B's skip_reason verbatim, and NO model call.
  * no artifact possible           -> inconclusive, "this technology leaves no artifact in source code". Not no_evidence.
  * authorship_confidence "none"   -> never supported: inconclusive (blame attributed every line to the committer).
  * likely_identity_gap True       -> never contradicted on authorship grounds: we may simply not recognise them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from rapidfuzz import fuzz

NO_ARTIFACT_REASON = "this technology leaves no artifact in source code"
SINGLE_COMMIT_REASON = ("authorship_confidence is none: blame attributed every line to the committer by construction "
                        "(single-commit import), so authorship cannot be established")


SCOPE_REASON = "evidence is not about the same scope as the claim"
HISTORY_COUNT_REASON = ("a repository's history or size cannot contradict a claim: Crucible sees a partial sample of "
                        "repositories, and absence in that sample is not evidence of absence")
_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str | None) -> list[str]:
    return _TOK.findall((s or "").lower().replace("_", " "))


_TARGET_STOP = frozenset("the and for with from into module service library package component engine layer client pipeline "
                         "api system tool job worker scheduler parser cli script".split())


def _components(path: str) -> list[str]:
    p = PurePosixPath(path)
    return [x.lower() for x in p.parts[:-1]] + [p.stem.lower(), p.name.lower()]


def _words(s: str | None) -> list[str]:
    """Whole words of a path or identifier: split on / . _ - space and camelCase boundaries."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s or "")
    return [w for w in re.split(r"[\s/._\-]+", s.lower()) if w]


def scope_match(cited, scope: str | None, target: str | None) -> tuple[bool, str, float | None]:
    """(match, rule, audit score). STRICT (Phase 4D G2): the scope target must appear in the cited item as a WHOLE path
    component or a WHOLE word of its path or function name; no fuzzy score ever qualifies. The rapidfuzz score of the
    best near-miss is recorded for audit only.

    Asymmetry, on purpose: retrieval may be generous, contradiction must be strict. A missed contradiction is
    recoverable; a false contradiction about a real person is not.

    For an OWNERSHIP claim, the contradiction also needs PROVENANCE (_contradiction_allowed): the cited item must come
    from r_ownership, the 4B rule that identified the artifact, and then ANY content word of scope_target may match.
    This replaced a first-distinctive-word heuristic (Phase 4H H0a), which read the object off word order and fails on
    "payment reconciliation service" (reconciler.py), "user authentication module" (auth.py) and "data migration
    script" (migrate.py). Identification is 4B's job and it already did it; 4C checks provenance, not word order.
    Note the whole-word requirement still blocks those three examples ("reconciliation" is not a whole word of
    reconciler.py): a missed contradiction, on the safe side."""
    if cited is None or not target or scope not in ("specific_artifact", "project"):
        return False, "no_target_or_scope", None
    path = getattr(cited, "path", None) or ""
    fn = re.search(r"function (\S+)", getattr(cited, "match_detail", None) or "")
    func_words = _words(fn.group(1)) if fn else []
    t = target.lower().strip()
    if scope == "specific_artifact" and ("/" in t or "." in t):  # a file named in the claim
        tt = t.lstrip("./")
        ok = path.lower() == tt or path.lower().endswith("/" + tt)
        return ok, "file_path" if ok else "file_path_mismatch", 100.0 if ok else None
    if scope == "project":
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        repo = (getattr(cited, "repo_full_name", "") or "").split("/")[-1]
        if norm(t) == norm(repo):
            return True, "project_repo_name", 100.0
        if any(norm(t) == norm(c) for c in PurePosixPath(path).parts[:-1]):
            return True, "project_path_component", 100.0
        return False, "project_mismatch", None
    words = [w for w in _words(t) if len(w) >= 3 and w not in _TARGET_STOP]
    if not words:
        return False, "empty_target", None
    comps, pwords = _components(path), _words(path)
    for w in words:  # ANY content word of the target, as a WHOLE path component or WHOLE word; never a fuzzy score
        if w in comps:
            return True, f"whole_path_component:{w}", 100.0
        if w in pwords:
            return True, f"whole_word_in_path:{w}", 100.0
        if w in func_words:
            return True, f"whole_word_in_function:{w}", 100.0
    best = max((fuzz.ratio(w, h) for w in words for h in pwords + func_words), default=None)
    return False, "no_whole_word_match", best


def same_object(cited, scope: str | None, target: str | None) -> bool:
    return scope_match(cited, scope, target)[0]


TECH_NOT_AUTHORED_REASON = "the technology is present but in code the candidate did not author"
PROVENANCE_REASON = ("an ownership claim can only be contradicted by evidence that retrieval identified as that artifact "
                     "(rule r_ownership); this citation came from a different rule")


def _contradiction_allowed(cited, scope: str | None, target: str | None,
                           claim_kind: str | None = None) -> tuple[bool, str, str, str, float | None]:
    if getattr(cited, "kind", None) in ("history", "count"):
        return False, HISTORY_COUNT_REASON, "gate:history_or_count_cannot_contradict", "history_or_count", None
    ok, rule, score = scope_match(cited, scope, target)  # first, so the matched word is recorded whatever happens next
    if not ok:
        return False, SCOPE_REASON, "gate:scope", rule, score
    if claim_kind == "ownership" and getattr(cited, "rule", None) != "r_ownership":
        return False, PROVENANCE_REASON, "gate:provenance", rule, score
    return True, "", "", f"{cited.rule}:{rule}" if claim_kind == "ownership" else rule, score


@dataclass
class Outcome:
    verdict: str
    reason: str
    row: str
    gates: list[str] = field(default_factory=list)
    authorship_note: str | None = None
    scope_match_rule: str | None = None  # recorded whenever the scope gate was evaluated (G2 audit)
    scope_match_score: float | None = None


def decide(content: str, claim_kind: str | None, cited, scope: str | None = None, scope_target: str | None = None) -> Outcome:
    """Final verdict from the model's content label, the claim's scope and the cited EvidenceItem's authorship fields
    (decided by 4B). Every path that would end in "contradicted" goes through the scope rule first."""
    out = _decide(content, claim_kind, cited)
    if out.verdict == "contradicted":
        ok, reason, gate, rule, score = _contradiction_allowed(cited, scope, scope_target, claim_kind)
        if not ok:
            return Outcome("inconclusive", reason, f"{out.row}->{gate}", out.gates + [gate], out.authorship_note, rule, score)
        out.scope_match_rule, out.scope_match_score = rule, score
    return out


def _decide(content: str, claim_kind: str | None, cited) -> Outcome:
    if content == "contradicted":
        return Outcome("contradicted", "the evidence contradicts the claim's content", "contradicted:any")
    if content == "no_evidence":
        return Outcome("no_evidence", "the search found nothing bearing on the claim's content", "no_evidence")
    if content != "supported":
        return Outcome("inconclusive", "the evidence does not let the content be determined", "inconclusive")

    authored = getattr(cited, "authored_by_candidate", None)
    confidence = getattr(cited, "authorship_confidence", None)
    gap = getattr(cited, "likely_identity_gap", None) is True
    if confidence == "none":
        return Outcome("inconclusive", SINGLE_COMMIT_REASON, "supported+confidence_none", ["gate:authorship_confidence_none"])
    if authored is True and not gap:
        return Outcome("supported", "the cited evidence supports the claim and the candidate authored it", "supported+authored")
    if authored is False and gap:
        return Outcome("inconclusive", "the cited code is not attributed to the candidate, but likely_identity_gap is set: "
                       "they may have written it under an email we do not recognise", "supported+not_authored+identity_gap",
                       ["gate:likely_identity_gap"])
    if authored is False:
        note = "the cited evidence exists in the repository but was not written by the candidate (blame)"
        if claim_kind == "ownership":
            return Outcome("contradicted", "the claim asserts ownership, and blame attributes the cited code to someone else",
                           "supported+not_authored+ownership", authorship_note=note)
        if claim_kind == "technology_use":
            # Phase 4K K1: "uses Python" is a claim about the candidate, not the repository. Measured: a row read
            # "Python: supported" because the repo contains Python, about a repo flagged as possibly someone else's.
            return Outcome("inconclusive", TECH_NOT_AUTHORED_REASON, "supported+not_authored+technology",
                           ["gate:technology_not_in_authored_code"], authorship_note=note)
        return Outcome("supported", "the claim asserts existence or use, which the evidence shows", "supported+not_authored+use",
                       authorship_note=note)
    if authored is True and gap:  # authored by a recognised identity even though the repo shows a gap: still authored
        return Outcome("supported", "the cited evidence supports the claim and the candidate authored it", "supported+authored")
    caveat = getattr(cited, "authorship_caveat", None)
    return Outcome("inconclusive", f"the content is supported but authorship of the cited evidence could not be measured"
                   f"{': ' + caveat if caveat else ''}", "supported+authorship_unknown")
