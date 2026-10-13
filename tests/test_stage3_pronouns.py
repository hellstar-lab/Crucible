"""Stage 3 items 1-2: first person never triggers disambiguation; disagreeing samples FAIL OPEN with a containment
filter, so no resolution (including an invented one) can enter a claim. Offline: FakeLLM, with the three samples
recorded live for the phase-II "automatic rollback triggers" sentence."""

from __future__ import annotations

import json

import pytest

from backend.claims import pipeline, stages
from test_claims_pipeline import J, fake  # noqa: F401  (the fixture)

ROLLBACK = ("Once cleared through these OS filters, queries reach our MySQL instance where transactions execute under strict "
            "isolation with automatic rollback triggers for race conditions or full classes.")
INVENTED = "transactions (student submissions)"
# the three samples recorded live (2026-10-13, Ultra): two different resolutions, one refusal
SAMPLES = [
    J("resolved", clarified_text="Once cleared through these OS filters (custom semaphores and thread pool governance), queries reach "
                                 "our MySQL instance (database storage) where transactions (student submissions) execute under strict "
                                 "isolation with automatic rollback triggers for race conditions or full classes.", failed_referent=None),
    J("resolved", clarified_text="Once cleared through these OS-level thread scheduling and queuing mechanisms, queries reach our MySQL "
                                 "database storage instance where transactions execute under strict isolation with automatic rollback "
                                 "triggers for race conditions or full classes.", failed_referent=None),
    J("cannot_be_disambiguated", clarified_text=None, failed_referent="these OS filters"),
]


@pytest.mark.parametrize("text", ["We are keeping strictly to our staged schedule.", "Our design utilizes a layered pipeline.",
                                  "I built my own scheduler.", "Version control is managed via our central GitHub repository."])
def test_first_person_never_triggers_stage_3(text):
    assert stages.needs_disambiguation(text) is None


def test_other_pronouns_still_trigger():
    assert stages.needs_disambiguation(ROLLBACK) == "these"
    assert stages.needs_disambiguation("It handles retries.") == "It"


def test_disagreeing_samples_fail_open_and_the_containment_filter_decides(fake):  # noqa: F811
    fake.script[("claim_disambiguation", ROLLBACK)] = SAMPLES
    fake.script[("claim_decomposition", ROLLBACK)] = J("decomposed", claims=[
        {"text": "Transactions execute under strict isolation with automatic rollback triggers for race conditions or full classes.",
         "qualifiers": []},
        {"text": "Queries reach the MySQL instance once cleared through these OS filters.", "qualifiers": []}])
    out = pipeline.extract_from_text(ROLLBACK, headings=["Updated Project Approach and Architecture"], decomposition_tier="nano")
    texts = [c.text for c in out.claims]
    assert texts == ["Transactions execute under strict isolation with automatic rollback triggers for race conditions or full classes."]
    assert not any("these OS filters" in t for t in texts)
    assert [(r.claim_text, r.reason_code) for r in out.rejected_claims] == [
        ("Queries reach the MySQL instance once cleared through these OS filters.", "unresolved_referent")]
    # nothing was rewritten: decomposition was given the ORIGINAL sentence, and the invented referent is nowhere
    decomposition = [p for task, _tier, p in fake.prompts if task == "claim_decomposition"]
    assert decomposition and all(ROLLBACK in p and INVENTED not in p for p in decomposition)
    assert out.units[0].clarified_text == ROLLBACK
    assert INVENTED not in json.dumps(out.model_dump(), ensure_ascii=False)  # not even in the Stage 3 audit record
    d3 = next(d for d in out.units[0].decisions if d.stage == "disambiguation")
    assert all(s.get("clarified_text") is None for s in d3.samples) and "every resolution discarded" in d3.reason


def test_a_unanimous_refusal_is_still_a_refusal(fake):  # noqa: F811
    unit = "This draws heavily from MD-ACC research patterns."
    fake.script[("claim_disambiguation", unit)] = J("cannot_be_disambiguated", clarified_text=None, failed_referent="MD-ACC")
    out = pipeline.extract_from_text(unit, headings=["Architecture"], decomposition_tier="nano")
    assert out.claims == [] and [e.reason_code for e in out.excluded] == ["cannot_be_disambiguated"]


def test_if_every_claim_carries_the_unresolved_span_the_unit_is_refused(fake):  # noqa: F811
    unit = "These filters reject malformed requests."
    fake.script[("claim_disambiguation", unit)] = [J("resolved", clarified_text="The OS filters reject malformed requests.", failed_referent=None),
                                                   J("resolved", clarified_text="The admission filters reject malformed requests.", failed_referent=None),
                                                   J("cannot_be_disambiguated", clarified_text=None, failed_referent="These filters")]
    out = pipeline.extract_from_text(unit, headings=["Architecture"], decomposition_tier="nano")
    assert out.claims == [] and [e.reason_code for e in out.excluded] == ["cannot_be_disambiguated"]
    assert "unresolved span" in out.excluded[0].detail
