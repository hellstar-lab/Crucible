"""Phase 4C verdicts, offline. The matrix is deterministic, so it is tested with MOCKED model output on the 4B scenario
repos; one test per matrix row. No API calls (llm.call is a fake; conftest removes the key)."""

from __future__ import annotations

import json

import pytest

from backend import llm, retrieval, verdicts
from backend.contracts import ClaimVerdict, EvidenceItem
from backend.retrieval import rules
from backend.verdicts import config as vconfig
from backend.verdicts import judge, matrix

from test_retrieval import build_scenarios, claim, view


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return build_scenarios(tmp_path_factory.mktemp("verdicts"))


class FakeJudge:
    """Answers the content question from a script; records tier and prompt of every call."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def __call__(self, task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None,
                 json_schema=None, tier=None, n=1):
        assert task == "verify_claim" and json_schema == judge.SCHEMA and temperature == 0 and thinking is False
        self.calls.append((tier, prompt))
        label, idx, prob, greedy = self.answers.pop(0) if self.answers else ("supported", 0, 0.95, False)
        data = {"evidence_summary": "summary", "content_reason": f"item {idx} shows it", "content_supported": label,
                "cited_item_index": idx}
        dist = {"values": {v: {"prob": (prob if v == label else round((1 - prob) / 3, 4))} for v in
                           ("supported", "contradicted", "no_evidence", "inconclusive")}, "argmax": label, "robust": True}
        return {"text": json.dumps(data), "data": data, "value_distributions": {"content_supported": dist},
                "greedy_disagreement": greedy, "cost_usd": 0.0007, "prompt_hash": "h"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeJudge()
    monkeypatch.setattr(llm, "call", f)
    return f


REDIS = claim("Built a caching layer with Redis")


def ev(scenarios, name, c):
    return retrieval.retrieve(c, [view(scenarios, name)])


def first_index(es, rule):
    return next(i for i, it in enumerate(es.items) if it.rule == rule)


# --------------------------------------------------------------------------
# one test per matrix row
# --------------------------------------------------------------------------


def test_supported_and_authored_is_supported(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("supported", first_index(es, "r_import"), 0.95, False)]
    v, calls, _ = verdicts.verdict_for(REDIS, es)
    assert v.verdict == "supported" and v.matrix_row == "supported+authored" and calls == 1 and v.model_tier == "super"


def test_single_commit_import_supported_content_is_inconclusive(scenarios, fake):
    es = ev(scenarios, "single_commit_import", REDIS)
    fake.answers = [("supported", first_index(es, "r_import"), 0.95, False)]
    v, _, _ = verdicts.verdict_for(REDIS, es)
    assert v.verdict == "inconclusive" and "gate:authorship_confidence_none" in v.gates
    assert "blame attributed every line to the committer by construction" in v.reason


@pytest.mark.parametrize("kind", ["technology_use", "ownership"])
def test_identity_gap_supported_content_is_inconclusive_never_contradicted(scenarios, fake, kind):
    c = claim("Built a caching layer with Redis", kind)
    es = retrieval.retrieve(claim("Built a caching layer with Redis"), [view(scenarios, "identity_gap")])  # Redis evidence
    fake.answers = [("supported", first_index(es, "r_import"), 0.95, False)]
    v, _, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "inconclusive" and v.verdict != "contradicted" and "gate:likely_identity_gap" in v.gates


def _ledger_evidence(scenarios, kind):
    v = view(scenarios, "clean_small")
    c = claim("Designed the ledger reconciliation module", kind)
    items = rules.r_ownership(c["text"], [], v).items  # Bob's ledger
    es = retrieval.retrieve(claim("Designed the ledger reconciliation module", "ownership"), [v])
    assert es.items and all(i.authored_by_candidate is False for i in es.items if i.path == "src/billing/ledger.py")
    assert items
    return c, es


def test_bobs_code_with_an_ownership_claim_is_contradicted(scenarios, fake):
    c, es = _ledger_evidence(scenarios, "ownership")
    idx = next(i for i, it in enumerate(es.items) if it.path == "src/billing/ledger.py")
    fake.answers = [("supported", idx, 0.95, False)]
    v, _, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "contradicted" and v.matrix_row == "supported+not_authored+ownership" and v.authorship_note


def test_bobs_code_with_a_use_claim_is_supported_with_an_authorship_note(scenarios, fake):
    c, es = _ledger_evidence(scenarios, "technology_use")
    idx = next(i for i, it in enumerate(es.items) if it.path == "src/billing/ledger.py")
    fake.answers = [("supported", idx, 0.95, False)]
    v, _, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "supported" and v.matrix_row == "supported+not_authored+use"
    assert "not written by the candidate" in v.authorship_note


def test_exclusion_stress_empty_items_is_no_evidence_without_a_call(scenarios, fake):
    es = ev(scenarios, "exclusion_stress", REDIS)
    assert es.items == [] and es.searched is not None
    v, calls, _ = verdicts.verdict_for(REDIS, es)
    assert v.verdict == "no_evidence" and calls == 0 and fake.calls == []


def test_role_or_team_is_unverifiable_with_no_model_call(scenarios, fake):
    c = claim("Led a team of 6 engineers", "role_or_team", checkable="no")
    es = ev(scenarios, "clean_small", c)
    v, calls, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "unverifiable" and v.reason == es.skip_reason and calls == 0 and fake.calls == []


def test_linux_claim_is_inconclusive_not_no_evidence(scenarios, fake):
    c = claim("Administered Linux servers")
    es = ev(scenarios, "clean_small", c)
    assert es.searched.technologies == ["linux"] and es.items == []
    v, calls, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "inconclusive" and v.reason == matrix.NO_ARTIFACT_REASON and calls == 0


@pytest.mark.parametrize("bad_index", [99, -1, None])
def test_out_of_range_or_missing_citation_is_downgraded_and_counted(scenarios, fake, bad_index):
    from collections import Counter
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("supported", bad_index, 0.95, False)]
    counters = Counter()
    v, _, _ = verdicts.verdict_for(REDIS, es, counters)
    assert v.verdict == "inconclusive" and v.citation_downgraded and counters["citation_downgraded"] == 1


def test_content_contradicted_is_contradicted_and_escalated_to_ultra(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("contradicted", 0, 0.9, False), ("contradicted", 0, 0.9, False)]
    v, calls, _ = verdicts.verdict_for(REDIS, es)
    assert v.verdict == "contradicted" and calls == 2 and v.escalated and v.model_tier == "ultra"
    assert [t for t, _p in fake.calls] == ["super", "ultra"] and v.escalation_reason == "content label contradicted"


def test_low_confidence_label_escalates_and_ultra_decides(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("supported", 0, 0.55, False), ("inconclusive", None, 0.8, False)]
    v, calls, _ = verdicts.verdict_for(REDIS, es)
    assert calls == 2 and v.escalated and "0.550 < 0.7" in v.escalation_reason and v.verdict == "inconclusive"


def test_no_evidence_and_inconclusive_content_pass_through(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    for label in ("no_evidence", "inconclusive"):
        fake.answers = [(label, None, 0.9, False)]
        assert verdicts.verdict_for(REDIS, es)[0].verdict == label


# --------------------------------------------------------------------------
# the model never sees authorship; no scores anywhere; cost cap
# --------------------------------------------------------------------------


def test_prompt_payload_carries_no_authorship_fields(scenarios, fake):
    for name in ("clean_small", "identity_gap", "single_commit_import"):
        es = ev(scenarios, name, REDIS)
        assert any(i.authorship_confidence is not None for i in es.items)  # the data exists...
        p = judge.prompt(REDIS["text"], es)
        for field in judge.FORBIDDEN_FIELDS:  # ...and never reaches the model
            assert field not in p, (name, field)
        for item in judge.payload(REDIS["text"], es)["items"]:
            assert not set(item) & set(judge.FORBIDDEN_FIELDS)
        assert "<<<UNTRUSTED_" in p and p.count("<<<UNTRUSTED_") == 2  # the claim and the repository evidence


def test_the_verdict_contract_has_no_scores():
    def walk(schema):
        props = schema.get("properties", {})
        for k, v in props.items():
            assert "score" not in k and "rank" not in k, k
        for d in schema.get("$defs", {}).values():
            for k in d.get("properties", {}):
                if d.get("title") != "EvidenceItem":  # 4B's retrieval ranking score lives on the evidence, not the verdict
                    assert "score" not in k and "rank" not in k, k
    walk(ClaimVerdict.model_json_schema())


def test_document_level_cost_cap_truncates_and_lists_unjudged_claims(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    claims, sets = [], []
    for i in range(4):
        c = {**REDIS, "claim_id": f"c{i}"}
        claims.append(c)
        sets.append(es.model_copy(update={"claim_id": f"c{i}"}))
    role = claim("Led a team", "role_or_team", "r1", "no")
    claims.append(role)
    sets.append(ev(scenarios, "clean_small", role))
    out = verdicts.verdicts_for(claims, sets, cost_cap_usd=0.0015)  # two judgements of 0.0007 fit
    assert out.truncated and out.not_judged == ["c2", "c3"] and out.calls == 3 - 1  # c0, c1 judged; role needs no call
    assert [v.claim_id for v in out.verdicts] == ["c0", "c1", "r1"] and out.verdicts[-1].verdict == "unverifiable"


def test_greedy_disagreement_rate_is_reported(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("supported", 0, 0.95, True), ("supported", 0, 0.95, False)]
    out = verdicts.verdicts_for([{**REDIS, "claim_id": "a"}, {**REDIS, "claim_id": "b"}],
                                [es.model_copy(update={"claim_id": "a"}), es.model_copy(update={"claim_id": "b"})])
    assert out.greedy_disagreement_rate == 0.5 and out.counters["judgements"] == 2
