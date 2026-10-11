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


def test_a_technology_only_in_bobs_code_is_inconclusive(scenarios, fake):
    """K1: a technology_use claim is about the candidate. Present only in code someone else wrote -> inconclusive."""
    c, es = _ledger_evidence(scenarios, "technology_use")
    idx = next(i for i, it in enumerate(es.items) if it.path == "src/billing/ledger.py")
    fake.answers = [("supported", idx, 0.95, False)]
    v, _, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "inconclusive" and v.matrix_row == "supported+not_authored+technology"
    assert v.reason == matrix.TECH_NOT_AUTHORED_REASON and "not written by the candidate" in v.authorship_note


def test_existence_claims_on_someone_elses_code_stay_supported_with_a_note():
    """artifact_exists ("has CI") is about the repository, so it keeps the old rule, with the authorship note."""
    item = EvidenceItem(repo_full_name="o/r", path=".github/workflows/ci.yml", kind="config", rule="r_structure",
                        authored_by_candidate=False, authorship_confidence="medium")
    out = matrix.decide("supported", "artifact_exists", item)
    assert out.verdict == "supported" and out.row == "supported+not_authored+use" and out.authorship_note


@pytest.mark.parametrize("authored,confidence,verdict", [(True, "medium", "supported"), (False, "medium", "inconclusive"),
                                                         (None, "medium", "inconclusive"), (True, "none", "inconclusive")])
def test_technology_use_authorship_ladder(authored, confidence, verdict):
    item = EvidenceItem(repo_full_name="o/r", path="src/a.py", kind="import", rule="r_import", technology="redis",
                        authored_by_candidate=authored, authorship_confidence=confidence)
    assert matrix.decide("supported", "technology_use", item).verdict == verdict


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
    assert v.scope_match_rule in ("r_ownership:whole_path_component:ledger", "r_ownership:whole_word_in_function:ledger") and v.scope_match_score == 100.0
    assert v.verdict == "contradicted" and calls == 2 and v.escalated and v.model_tier == "ultra"
    assert [t for t, _p, _n in fake.calls] == ["super", "ultra"] and v.escalation_reason.startswith("unanimous verdict contradicted")
    assert len(v.samples) == 6  # 3 Super + 3 Ultra, all recorded


# --- I1: a contradiction needs Super 3/3 AND Ultra 3/3 ------------------------------------------------------------

def _ledger_idx(es):
    return next(i for i, it in enumerate(es.items) if it.path == "src/billing/ledger.py")


@pytest.mark.parametrize("name,super_ans,ultra_ans", [
    # the measured run-2 case: Super held back 2/3, Ultra alone would have made it a contradiction
    ("ultra_overrules_super_majority", [("inconclusive", None, 0.9, False)] * 2 + [("contradicted", "IDX", 0.9, False)],
     ("contradicted", "IDX", 0.95, False)),
    # (labels are content labels; the matrix runs per sample, so "supported" on Bob's ledger is already a
    #  contradiction after blame; these cases use labels that stay non-contradicting after the matrix)
    ("ultra_overrules_unanimous_low_prob_super", ("inconclusive", None, 0.55, False), ("contradicted", "IDX", 0.95, False)),
    ("ultra_does_not_confirm_super", ("contradicted", "IDX", 0.9, False), ("inconclusive", None, 0.9, False)),
    ("ultra_confirms_only_2_of_3", ("contradicted", "IDX", 0.9, False),
     [("contradicted", "IDX", 0.9, False)] * 2 + [("inconclusive", None, 0.9, False)]),
    ("ultra_says_no_evidence_after_super_contradicted", ("contradicted", "IDX", 0.9, False), ("no_evidence", None, 0.9, False)),
])
def test_contradiction_without_both_tiers_unanimous_is_inconclusive_with_both_sample_sets(scenarios, fake, name, super_ans, ultra_ans):
    c, es = _ledger_evidence(scenarios, "ownership")
    i = _ledger_idx(es)
    fix = lambda a: tuple(i if x == "IDX" else x for x in a)  # noqa: E731
    fake.answers = [[fix(a) for a in super_ans] if isinstance(super_ans, list) else fix(super_ans),
                    [fix(a) for a in ultra_ans] if isinstance(ultra_ans, list) else fix(ultra_ans)]
    from collections import Counter
    counters = Counter()
    v, calls, _ = verdicts.verdict_for(c, es, counters)
    assert v.verdict == "inconclusive" and v.matrix_row == "contradiction:not_confirmed" and calls == 2
    assert "gate:contradiction_needs_both_tiers" in v.gates and v.reason.startswith("not a contradiction")
    assert [s["tier"] for s in v.samples] == ["super"] * 3 + ["ultra"] * 3  # the near miss is auditable
    assert v.cited_item_index == i and counters["contradictions_not_confirmed"] == 1 and not counters["contradictions_confirmed"]
    assert not counters["disagreements_resolved_by_ultra"]  # a near miss is not a resolution


def test_contradiction_confirmed_by_both_tiers(scenarios, fake):
    c, es = _ledger_evidence(scenarios, "ownership")
    i = _ledger_idx(es)
    fake.answers = [("contradicted", i, 0.9, False), ("contradicted", i, 0.95, False)]
    from collections import Counter
    counters = Counter()
    v, calls, _ = verdicts.verdict_for(c, es, counters)
    assert v.verdict == "contradicted" and calls == 2 and counters["contradictions_confirmed"] == 1


def test_super_disagreement_resolved_to_supported_by_ultra_is_unchanged(scenarios, fake):
    """I1 touches contradictions only: other labels still resolve through Ultra as before."""
    es = ev(scenarios, "clean_small", REDIS)
    i = first_index(es, "r_import")
    fake.answers = [[("supported", i, 0.9, False), ("contradicted", i, 0.9, False), ("supported", i, 0.9, False)],
                    ("supported", i, 0.95, False)]
    v, calls, _ = verdicts.verdict_for(REDIS, es)
    assert v.verdict == "supported" and calls == 2


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


AUDIT_ONLY = {"scope_match_score"}  # scores a STRING match (scope target vs cited path), never the candidate


def test_the_verdict_contract_has_no_scores():
    def walk(schema):
        props = schema.get("properties", {})
        for k, v in props.items():
            if k in AUDIT_ONLY:
                continue
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


def test_degree_dates_never_reach_a_model(scenarios, fake):
    """The measured failure: "2014 - 2018" came back contradicted in 3 of 3 runs, citing one repo's 2026 history.
    J3: a career-scope timeline claim is not searched and no model sees it."""
    c = claim("Completed the degree between 2014 and 2018", "timeline")
    es = retrieval.retrieve(c, [view(scenarios, "clean_small")])
    assert not es.retrieval_performed and es.skip_reason.startswith("career-scope timeline claim")
    v, calls, _ = verdicts.verdict_for(c, es)
    assert v.verdict == "unverifiable" and calls == 0 and fake.calls == []


def test_history_gate_still_holds_if_a_career_claim_slips_past_routing(scenarios, fake):
    """Defence in depth, pinned: evidence forced past the J3 routing rule (retrieved as if project-scoped) and a MOCKED
    unanimous contradicted must still come back inconclusive at the 4C history/count gate."""
    from datetime import date
    c = claim("2014 - 2018", "timeline")
    es = retrieval.retrieve({**c, "scope": "project"}, [view(scenarios, "clean_small")], today=date(2026, 10, 11))
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
    # H0a: an ownership contradiction also needs r_ownership provenance, so the matching case uses such an item
    owned = item.model_copy(update={"kind": "file", "rule": "r_ownership"})
    assert matrix.decide("contradicted", "ownership", owned, "specific_artifact", "src/x.py").verdict == "contradicted"
    assert matrix.decide("contradicted", "ownership", owned, "specific_artifact", "src/y.py").verdict == "inconclusive"
    assert matrix.decide("contradicted", "ownership", item, "specific_artifact", "src/x.py").verdict == "inconclusive"



def test_super_disagreement_escalates_and_ultra_disagreement_is_inconclusive_with_both_sample_sets(scenarios, fake):
    """H0b: disagreement escalates instead of abstaining; only Ultra disagreeing too makes it inconclusive."""
    es = ev(scenarios, "clean_small", REDIS)
    i = first_index(es, "r_import")
    sup = [("supported", i, 0.996, False), ("no_evidence", None, 0.997, False), ("inconclusive", None, 0.876, False)]
    fake.answers = [sup, [("supported", i, 0.9, False), ("supported", i, 0.9, False), ("no_evidence", None, 0.9, False)]]
    from collections import Counter
    counters = Counter()
    v, calls, _ = verdicts.verdict_for(REDIS, es, counters)
    assert [t for t, _p, _n in fake.calls] == ["super", "ultra"] and calls == 2 and v.escalated
    assert v.verdict == "inconclusive" and v.reason.startswith("verdicts disagreed (Super and Ultra")
    assert [(s["tier"], s["content_supported"]) for s in v.samples] == [
        ("super", "supported"), ("super", "no_evidence"), ("super", "inconclusive"),
        ("ultra", "supported"), ("ultra", "supported"), ("ultra", "no_evidence")]
    assert v.escalation_reason.startswith("Super samples disagreed") and v.agreement == "2/3"  # Ultra's 2/3 is NOT a majority win
    assert counters["ensemble_disagreements"] == 1 and counters["escalations_on_disagreement"] == 1
    assert counters["disagreements_resolved_by_ultra"] == 0


def test_super_disagreement_resolved_by_a_unanimous_ultra(scenarios, fake):
    es = ev(scenarios, "clean_small", REDIS)
    i = first_index(es, "r_import")
    fake.answers = [[("supported", i, 0.95, False), ("supported", i, 0.95, False), ("no_evidence", None, 0.9, False)],
                    ("supported", i, 0.97, False)]
    from collections import Counter
    counters = Counter()
    v, calls, _ = verdicts.verdict_for(REDIS, es, counters)
    assert v.verdict == "supported" and v.model_tier == "ultra" and calls == 2 and len(v.samples) == 6
    assert counters["disagreements_resolved_by_ultra"] == 1 and counters.get("ensemble_disagreements", 0) == 0


# --- Phase 4L L1: technology_use authorship from the whole EvidenceSet ---------------------------------------------

def _httpx_set():
    from backend.contracts import EvidenceSet, SearchedBlock
    dep = EvidenceItem(repo_full_name="o/r", path="requirements.txt", start_line=6, end_line=6, kind="dependency",
                       rule="r_dependency", technology="httpx", authored_by_candidate=False, authorship_confidence="low")
    imp = EvidenceItem(repo_full_name="o/r", path="src/client.py", start_line=1, end_line=1, kind="import", rule="r_import",
                       technology="httpx", authored_by_candidate=True, authorship_confidence="low",
                       volume={"basis": "authored_files_importing", "technology": "httpx", "files": 4, "authored_files": 4})
    return EvidenceSet(claim_id="s.c5", claim_kind="technology_use", retrieval_performed=True, retrieval_version="t",
                       searched=SearchedBlock(technologies=["httpx"]), items=[dep, imp])


@pytest.mark.parametrize("cited", [0, 1])
def test_httpx_verdict_does_not_depend_on_which_item_is_cited(fake, cited):
    """The measured flip: citing the requirements.txt line (not authored) used to give inconclusive."""
    es = _httpx_set()
    fake.answers = [("supported", cited, 0.95, False)]
    v, _, _ = verdicts.verdict_for({"claim_id": "s.c5", "text": "httpx", "kind": "technology_use"}, es)
    assert v.verdict == "supported" and v.cited_item_index == cited
    assert v.set_authorship["authored_items"] == 1 and v.set_authorship["not_authored_items"] == 1


def test_ownership_still_uses_the_cited_item():
    es = _httpx_set()
    sa = matrix.set_authorship(es.items)
    out = matrix.decide("supported", "ownership", es.items[0], "specific_artifact", "requirements", authorship=sa)
    assert out.verdict != "supported"  # the cited file is not the candidate's; the set does not rescue an ownership claim


def test_set_authorship_ladder():
    items = _httpx_set().items
    assert matrix.set_authorship(items).authored_by_candidate is True
    assert matrix.set_authorship(items[:1]).authored_by_candidate is False
    unknown = items[0].model_copy(update={"authored_by_candidate": None, "authorship_caveat": "no blame"})
    assert matrix.set_authorship([items[0], unknown]).authored_by_candidate is None  # unknown beats "not authored"
    assert matrix.decide("supported", "technology_use", items[0], authorship=matrix.set_authorship(items[:1])).verdict == "inconclusive"
