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
SAME_OBJECT_CUTOFF = 85  # rapidfuzz ratio, scope target word vs cited path / function token
_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str | None) -> list[str]:
    return _TOK.findall((s or "").lower().replace("_", " "))


def same_object(cited, scope: str | None, target: str | None) -> bool:
    """Does the cited evidence item belong to the artifact or project the claim names?"""
    if cited is None or not target or scope not in ("specific_artifact", "project"):
        return False
    path = getattr(cited, "path", None) or ""
    repo = (getattr(cited, "repo_full_name", "") or "").split("/")[-1]
    if scope == "specific_artifact" and ("/" in target or "." in target):  # a file named in the claim
        t = target.lower().lstrip("./")
        return path.lower() == t or path.lower().endswith("/" + t) or PurePosixPath(path).name.lower() == PurePosixPath(t).name
    if scope == "project":
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        return norm(target) in (norm(repo), *(norm(p) for p in PurePosixPath(path).parts[:-1]))
    # a component named in words ("ledger reconciliation"): at least one distinctive target word (generic words are
    # already removed by scope_of) must match a token of the cited item's path or function (match_detail names it)
    have = _tokens(path) + _tokens(getattr(cited, "match_detail", None))
    words = [w for w in _tokens(target) if len(w) >= 4] or _tokens(target)
    return any(fuzz.ratio(w, h) >= SAME_OBJECT_CUTOFF for w in words for h in have)


def _contradiction_allowed(cited, scope: str | None, target: str | None) -> tuple[bool, str, str]:
    if getattr(cited, "kind", None) in ("history", "count"):
        return False, HISTORY_COUNT_REASON, "gate:history_or_count_cannot_contradict"
    if scope not in ("specific_artifact", "project") or not same_object(cited, scope, target):
        return False, SCOPE_REASON, "gate:scope"
    return True, "", ""


@dataclass
class Outcome:
    verdict: str
    reason: str
    row: str
    gates: list[str] = field(default_factory=list)
    authorship_note: str | None = None


def decide(content: str, claim_kind: str | None, cited, scope: str | None = None, scope_target: str | None = None) -> Outcome:
    """Final verdict from the model's content label, the claim's scope and the cited EvidenceItem's authorship fields
    (decided by 4B). Every path that would end in "contradicted" goes through the scope rule first."""
    out = _decide(content, claim_kind, cited)
    if out.verdict == "contradicted":
        ok, reason, gate = _contradiction_allowed(cited, scope, scope_target)
        if not ok:
            return Outcome("inconclusive", reason, f"{out.row}->{gate}", out.gates + [gate], out.authorship_note)
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
        return Outcome("supported", "the claim asserts existence or use, which the evidence shows", "supported+not_authored+use",
                       authorship_note=note)
    if authored is True and gap:  # authored by a recognised identity even though the repo shows a gap: still authored
        return Outcome("supported", "the cited evidence supports the claim and the candidate authored it", "supported+authored")
    caveat = getattr(cited, "authorship_caveat", None)
    return Outcome("inconclusive", f"the content is supported but authorship of the cited evidence could not be measured"
                   f"{': ' + caveat if caveat else ''}", "supported+authorship_unknown")
