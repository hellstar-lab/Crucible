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
    """Answers the content question from a script; records tier and prompt of every request. An answer is one
    (label, cited_index, prob, greedy) tuple (used for all n samples) or a list of n such tuples."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    @staticmethod
    def _sample(label, idx, prob, greedy):
        data = {"evidence_summary": "summary", "content_reason": f"item {idx} shows it", "content_supported": label,
                "cited_item_index": idx}
        dist = {"values": {v: {"prob": (prob if v == label else round((1 - prob) / 3, 4))} for v in
                           ("supported", "contradicted", "no_evidence", "inconclusive")}, "argmax": label, "robust": True}
        return data, dist, greedy

    def __call__(self, task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None,
                 json_schema=None, tier=None, n=1):
        assert task == "verify_claim" and json_schema == judge.SCHEMA and thinking is False
        assert (n == 3 and temperature == 0.7) or (n == 1 and temperature == 0)
        self.calls.append((tier, prompt, n))
        ans = self.answers.pop(0) if self.answers else ("supported", 0, 0.95, False)
        per = ans if isinstance(ans, list) else [ans] * n
        samples = [self._sample(*a) for a in per]
        choices = [{"text": json.dumps(d), "data": d, "value_distributions": {"content_supported": dist}, "greedy_disagreement": g,
                    "error": None} for d, dist, g in samples]
        d0, dist0, g0 = samples[0]
        return {"text": json.dumps(d0), "data": d0, "value_distributions": {"content_supported": dist0}, "greedy_disagreement": g0,
                "cost_usd": 0.0007, "prompt_hash": "h", "choices": choices if n > 1 else None}


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
    assert v.agreement == "3/3" and len(v.samples) == 3 and fake.calls[0][2] == 3


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
    assert v.verdict == "inconclusive" and v.citation_downgraded and counters["citation_downgraded"] == 3  # one per sample


def test_unanimous_contradiction_on_the_same_object_escalates_to_ultra(scenarios, fake):
    c, es = _ledger_evidence(scenarios, "ownership")
    idx = next(i for i, it in enumerate(es.items) if it.path == "src/billing/ledger.py")
    fake.answers = [("contradicted", idx, 0.9, False), ("contradicted", idx, 0.9, False)]
    v, calls, _ = verdicts.verdict_for(c, es)
    assert v.scope == "specific_artifact" and v.scope_target == "ledger reconciliation"
    assert v.verdict == "contradicted" and calls == 2 and v.escalated and v.model_tier == "ultra"
    assert [t for t, _p, _n in fake.calls] == ["super", "ultra"] and v.escalation_reason == "unanimous verdict contradicted"
    assert len(v.samples) == 6  # 3 Super + 3 Ultra, all recorded


def test_low_confidence_label_escalates_and_ultra_decides(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    fake.answers = [("supported", first_index(es, "r_import"), 0.55, False), ("inconclusive", None, 0.8, False)]
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
    cap = 2 * vconfig.SUPER_REFERENCE_CALL_USD  # projected spend: two ensemble requests fit, a third would not
    out = verdicts.verdicts_for(claims, sets, cost_cap_usd=cap)
    assert out.truncated and out.not_judged == ["c2", "c3"] and out.calls == 2  # c0, c1 judged; the role claim needs no call
    assert [v.claim_id for v in out.verdicts] == ["c0", "c1", "r1"] and out.verdicts[-1].verdict == "unverifiable"


def test_greedy_disagreement_rate_is_reported(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    i = first_index(es, "r_import")
    fake.answers = [[("supported", i, 0.95, True), ("supported", i, 0.95, False), ("supported", i, 0.95, False)],
                    ("supported", i, 0.95, False)]
    out = verdicts.verdicts_for([{**REDIS, "claim_id": "a"}, {**REDIS, "claim_id": "b"}],
                                [es.model_copy(update={"claim_id": "a"}), es.model_copy(update={"claim_id": "b"})])
    assert out.greedy_disagreement_rate == round(1 / 6, 4) and out.counters["samples"] == 6  # per sample, not per claim



# --------------------------------------------------------------------------
# F1: scope rule; F2: ensemble unanimity
# --------------------------------------------------------------------------


def test_degree_dates_are_never_contradicted_by_a_repository_history(scenarios, fake):
    """The measured failure: "2014 - 2018" came back contradicted in 3 of 3 runs, citing one repo's 2026 history."""
    from datetime import date
    c = claim("2014 - 2018", "timeline")
    es = retrieval.retrieve(c, [view(scenarios, "clean_small")], today=date(2026, 10, 11))
    hist = next(i for i, it in enumerate(es.items) if it.kind == "history")
    fake.answers = [("contradicted", hist, 1.0, False)]
    v, calls, _ = verdicts.verdict_for(c, es)
    assert v.scope == "career" and v.verdict == "inconclusive" and v.verdict != "contradicted"
    assert "gate:history_or_count_cannot_contradict" in v.gates and calls == 1  # no escalation: nothing consequential left


def test_ownership_claim_contradicted_by_an_unrelated_config_file_is_inconclusive(scenarios, fake):
    """The case a history/count gate would have missed: a specific_artifact claim, evidence about a different object."""
    c = claim("Designed the ledger reconciliation module", "ownership")
    es = retrieval.retrieve(claim("Deployed it on Kubernetes"), [view(scenarios, "clean_small")])  # k8s / Docker config items
    cfg = next(i for i, it in enumerate(es.items) if it.rule == "r_config")
    assert "ledger" not in es.items[cfg].path
    fake.answers = [("contradicted", cfg, 0.95, False)]
    v, _, _ = verdicts.verdict_for(c, es)
    assert v.scope == "specific_artifact" and v.verdict == "inconclusive" and "gate:scope" in v.gates
    assert v.reason == matrix.SCOPE_REASON


@pytest.mark.parametrize("text,kind,scope", [
    ("2014 - 2018", "timeline", "career"), ("Backend engineer with 6 years of experience", "timeline", "career"),
    ("Maintained 300k lines of code", "scale", "career"), ("Built a caching layer with Redis", "technology_use", "none"),
    ("Designed the ledger reconciliation module", "ownership", "specific_artifact"), ("Wrote src/cache.py", "ownership", "specific_artifact"),
])
def test_scope_is_set_deterministically(text, kind, scope):
    from backend.claims.routing import scope_of
    assert scope_of(text, kind)[0] == scope


def test_scope_none_or_career_claims_can_never_be_contradicted():
    item = EvidenceItem(repo_full_name="o/r", path="src/x.py", kind="import", rule="r_import", authored_by_candidate=True)
    for scope in ("none", "career"):
        assert matrix.decide("contradicted", "technology_use", item, scope, None).verdict == "inconclusive"
    assert matrix.decide("contradicted", "ownership", item, "specific_artifact", "src/x.py").verdict == "contradicted"
    assert matrix.decide("contradicted", "ownership", item, "specific_artifact", "src/y.py").verdict == "inconclusive"


def test_ensemble_disagreement_is_inconclusive_and_every_sample_is_recorded(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    i = first_index(es, "r_import")
    fake.answers = [[("supported", i, 0.996, False), ("no_evidence", None, 0.997, False), ("inconclusive", None, 0.876, False)]]
    from collections import Counter
    counters = Counter()
    v, calls, _ = verdicts.verdict_for(REDIS, es, counters)
    assert v.verdict == "inconclusive" and v.reason == "verdicts disagreed" and calls == 1
    assert [s["content_supported"] for s in v.samples] == ["supported", "no_evidence", "inconclusive"]
    assert v.agreement == "1/3" and counters["ensemble_disagreements"] == 1 and not v.escalated  # no escalation without unanimity
