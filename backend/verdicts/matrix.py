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

HARD GATES, in code:
  * retrieval_performed False      -> unverifiable, 4B's skip_reason verbatim, and NO model call.
  * no artifact possible           -> inconclusive, "this technology leaves no artifact in source code". Not no_evidence.
  * authorship_confidence "none"   -> never supported: inconclusive (blame attributed every line to the committer).
  * likely_identity_gap True       -> never contradicted on authorship grounds: we may simply not recognise them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NO_ARTIFACT_REASON = "this technology leaves no artifact in source code"
SINGLE_COMMIT_REASON = ("authorship_confidence is none: blame attributed every line to the committer by construction "
                        "(single-commit import), so authorship cannot be established")


@dataclass
class Outcome:
    verdict: str
    reason: str
    row: str
    gates: list[str] = field(default_factory=list)
    authorship_note: str | None = None


def decide(content: str, claim_kind: str | None, cited) -> Outcome:
    """Final verdict from the model's content label and the cited EvidenceItem's authorship fields (decided by 4B)."""
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
