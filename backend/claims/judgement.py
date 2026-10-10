"""The judgement contract every model decision in Crucible uses (4A stages now; verdict phases later).

Every judgement response carries, IN THIS PROPERTY ORDER (reason-first, Phase 4A close-out C2):

    sufficiency_reason  : str
    evidence_sufficient : bool
    reason              : str
    verdict             : supported | contradicted | no_evidence | inconclusive
    (then any stage-specific fields)

Each answer is generated AFTER its justification (generation follows property order, measured 117/117). Measured on
4A Stage 3 (P2-4): reason-first took P(cannot_be_disambiguated) on w3 from 0.53-0.62 to 1.0 on every run, and
collapsed w4's 4 distinct outcomes to 1. ``reason_first=False`` keeps the old order for A/B.

and then, in code, unconditionally:  evidence_sufficient is False  ->  verdict = "inconclusive".

Verdict values (P2-2), one distinct first token each (sup / con / no / inc):
  supported     the evidence establishes the claim
  contradicted  the evidence establishes the opposite
  no_evidence   we looked and found nothing (consistent with Finding.status)
  inconclusive  we cannot determine it from the available evidence
Distinct first tokens because measured (P1a, P2-0) shared prefixes decide the answer: "unsupported" and "unclear" share
"un", nano generated "unclear" at p=0.302 while "unsupported" held 0.696, and when "unclear" was missing from the
enum a model starting the word "unclear" could only finish it as "unsupported". llm.call also re-ranks enum fields
over full sequence probabilities (P2-1), so the generated token path no longer decides the value on its own.

Why the override is code and not a schema or a prompt: model abstention is tier-dependent, and a boolean has no
synonym or shared prefix the model can drift into, so the sufficiency question is asked as a boolean and the
abstention is applied here.

Every case where the model wrote a non-"inconclusive" verdict alongside evidence_sufficient=False is a DISAGREEMENT:
it is recorded on the decision and counted, and the per-document disagreement rate is a reported metric.

Every judgement prompt carries a RUBRIC that defines all labels, with no hint about which applies to the instance.
Defining labels is not the T3 confound; telling the model the answer was.
"""

from __future__ import annotations

import json

INCONCLUSIVE = "inconclusive"
VERDICTS = ("supported", "contradicted", "no_evidence", "inconclusive")
JUDGEMENT_KEYS = ("sufficiency_reason", "evidence_sufficient", "reason", "verdict")
JUDGEMENT_KEYS_VERDICT_FIRST = ("evidence_sufficient", "sufficiency_reason", "verdict", "reason")


def judgement_schema(verdicts: list[str] | tuple[str, ...] = VERDICTS, extra: dict | None = None, reason_first: bool = True) -> dict:
    """Strict JSON schema: the four judgement fields first, in JUDGEMENT_KEYS order, then ``extra`` properties."""
    if INCONCLUSIVE not in verdicts:
        raise ValueError("every judgement verdict enum must include 'inconclusive' (contracts.py ENUM DESIGN RULE)")
    firsts = [v[:1] for v in verdicts]  # first CHARACTER: tokenisers split short prefixes such as "un" or "s" on their own
    if len(set(firsts)) != len(firsts):
        raise ValueError(f"verdict values must start with different characters (shared prefixes decide the answer): {list(verdicts)}")
    fields = {
        "evidence_sufficient": {"type": "boolean"},
        "sufficiency_reason": {"type": "string"},
        "verdict": {"type": "string", "enum": list(verdicts)},
        "reason": {"type": "string"},
    }
    props = {k: fields[k] for k in (JUDGEMENT_KEYS if reason_first else JUDGEMENT_KEYS_VERDICT_FIRST)}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def apply_override(data: dict) -> tuple[str, bool]:
    """(final verdict, disagreement). evidence_sufficient False forces "inconclusive"; a different model verdict
    next to it is a disagreement."""
    model_verdict = data.get("verdict")
    if data.get("evidence_sufficient") is False:
        return INCONCLUSIVE, model_verdict != INCONCLUSIVE
    return model_verdict, False


def key_order(raw_text: str) -> list[str] | None:
    """Top-level keys in the order the raw reply wrote them (json.loads preserves order); None if unparseable."""
    try:
        obj = json.loads(raw_text)
    except (TypeError, ValueError):
        return None
    return list(obj) if isinstance(obj, dict) else None


def follows_schema_order(order: list[str] | None, schema: dict) -> bool | None:
    return None if order is None else order == list(schema["properties"])[: len(order)]
