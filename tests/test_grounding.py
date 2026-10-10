"""Phase 4D grounding, offline: mocked verdicts on the 4B scenario repos. No model calls (llm.call fails the test)."""

from __future__ import annotations

from collections import Counter

import pytest

from backend import grounding, llm, retrieval
from backend.contracts import ClaimVerdict
from backend.grounding.report import build_report, render
from backend.verdicts import matrix

from test_retrieval import build_scenarios, claim, view


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a model call leaked into 4D")
    monkeypatch.setattr(llm, "call", boom)


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return build_scenarios(tmp_path_factory.mktemp("grounding"))


def verdict(c, es, idx, verdict="supported", content="supported") -> ClaimVerdict:
    return ClaimVerdict(claim_id=c["claim_id"], claim_text=c["text"], claim_kind=c["kind"], verdict=verdict, reason="mocked 4C",
                        matrix_row="mock", content_supported=content, cited_item_index=idx,
                        cited_item=es.items[idx] if isinstance(idx, int) and 0 <= idx < len(es.items) else None)


REDIS = claim("Built a caching layer with Redis")
OWN_CACHE = claim("Built the cache client", "ownership", "own1")
LEDGER = claim("Designed the ledger reconciliation module", "ownership", "own2")


def setup(scenarios, c):
    v = view(scenarios, "clean_small")
    return v, retrieval.retrieve(c, [v])


def test_citation_index_out_of_range_downgrades_on_c_index(scenarios):
    v, es = setup(scenarios, REDIS)
    failures = Counter()
    g = grounding.ground(verdict(REDIS, es, 42), es, [v], failures)
    assert g.verdict == "inconclusive" and g.pre_grounding_verdict == "supported" and g.grounding_checks["c_index"] == "fail"
    assert g.reason.startswith("grounding check c_index failed") and failures == {"c_index": 1}


def test_cited_path_not_in_the_snapshot_downgrades_on_c_path(scenarios):
    v, es = setup(scenarios, REDIS)
    i = next(k for k, it in enumerate(es.items) if it.rule == "r_import")
    forged = es.model_copy(update={"items": [it.model_copy(update={"path": "src/invented.py"}) if k == i else it
                                             for k, it in enumerate(es.items)]})
    g = grounding.ground(verdict(REDIS, forged, i), forged, [v])
    assert g.verdict == "inconclusive" and g.grounding_checks["c_path"] == "fail" and "src/invented.py" in g.reason


def test_line_range_past_end_of_file_downgrades_on_c_lines(scenarios):
    v, es = setup(scenarios, REDIS)
    i = next(k for k, it in enumerate(es.items) if it.rule == "r_import")
    forged = es.model_copy(update={"items": [it.model_copy(update={"start_line": 400, "end_line": 402}) if k == i else it
                                             for k, it in enumerate(es.items)]})
    g = grounding.ground(verdict(REDIS, forged, i), forged, [v])
    assert g.verdict == "inconclusive" and g.grounding_checks["c_lines"] == "fail" and "400..402" in g.reason


def test_ownership_supported_on_lines_someone_else_wrote_downgrades_on_c_authored(scenarios):
    v, es = setup(scenarios, LEDGER)
    i = next(k for k, it in enumerate(es.items) if it.kind == "function" and it.path == "src/billing/ledger.py")
    g = grounding.ground(verdict(LEDGER, es, i), es, [v])  # a (wrong) supported ownership verdict on Bob's function
    assert g.verdict == "inconclusive" and g.grounding_checks["c_authored"] == "fail"
    assert "the candidate authored 0 of" in g.reason


def test_item_with_no_line_range_records_not_applicable_and_the_verdict_stands(scenarios):
    v, es = setup(scenarios, claim("Deployed it on Kubernetes with Docker"))
    i = next(k for k, it in enumerate(es.items) if it.start_line is None and it.path == "Dockerfile")
    c = claim("Deployed it on Kubernetes with Docker")
    g = grounding.ground(verdict(c, es, i), es, [v])
    assert g.grounding_checks["c_lines"] == "not_applicable" and g.grounding_passed is True and g.verdict == "supported"


def test_all_four_pass_and_the_verdict_stands(scenarios):
    v, es = setup(scenarios, OWN_CACHE)
    i = next(k for k, it in enumerate(es.items) if it.kind == "function" and it.path == "src/cache.py")
    g = grounding.ground(verdict(OWN_CACHE, es, i), es, [v])
    assert g.grounding_checks == {"c_index": "pass", "c_path": "pass", "c_lines": "pass", "c_authored": "pass"}
    assert g.grounding_passed is True and g.verdict == "supported" and g.pre_grounding_verdict is None


def test_fuzzy_only_scope_match_cannot_contradict(scenarios):
    """G2: "reconciliation" vs reconcile_ledger scores 78 with rapidfuzz: close, not a whole word -> blocked."""
    v, es = setup(scenarios, LEDGER)
    item = next(it for it in es.items if it.kind == "function" and "reconcile_ledger" in (it.match_detail or ""))
    out = matrix.decide("supported", "ownership", item, "specific_artifact", "reconciliation")
    assert out.verdict == "inconclusive" and "gate:scope" in out.gates
    assert out.scope_match_rule == "no_whole_word_match" and out.scope_match_score is not None and out.scope_match_score < 100


def test_bobs_ledger_real_contradiction_is_still_contradicted_and_grounds(scenarios):
    from backend.claims.routing import scope_of
    v, es = setup(scenarios, LEDGER)
    scope, target = scope_of(LEDGER["text"], "ownership")
    assert (scope, target) == ("specific_artifact", "ledger reconciliation")  # H0a: the whole phrase; ANY word may match
    i = next(k for k, it in enumerate(es.items) if it.kind == "function" and it.path == "src/billing/ledger.py")
    out = matrix.decide("supported", "ownership", es.items[i], scope, target)
    assert out.verdict == "contradicted" and out.scope_match_rule in ("r_ownership:whole_path_component:ledger", "r_ownership:whole_word_in_function:ledger")
    vd = verdict(LEDGER, es, i, verdict="contradicted").model_copy(update={"scope": scope, "scope_target": target})
    g = grounding.ground(vd, es, [v])
    assert g.verdict == "contradicted" and g.grounding_passed is True and g.grounding_checks["c_authored"] == "not_applicable"


def test_unground_able_verdicts_pass_through_untouched(scenarios):
    v, es = setup(scenarios, REDIS)
    for content in ("no_evidence", "inconclusive", None):
        vd = verdict(REDIS, es, None, verdict="inconclusive", content=content)
        assert grounding.ground(vd, es, [v]) == vd


def test_report_assembles_without_computing(scenarios):
    v, es = setup(scenarios, OWN_CACHE)
    i = next(k for k, it in enumerate(es.items) if it.kind == "function" and it.path == "src/cache.py")
    role = claim("Led a team of 6", "role_or_team", "r1", "no")
    es_role = retrieval.retrieve(role, [v])
    grounded, failures = grounding.ground_all([verdict(OWN_CACHE, es, i)], [es, es_role], [v])
    report = build_report(verdicts=grounded, evidence_sets=[es, es_role], target_reports=[v.report], grounding_failures=failures)
    row = report.rows[0]
    item = es.items[i]
    assert row.citation == {"repo": item.repo_full_name, "path": item.path, "start_line": item.start_line, "end_line": item.end_line}
    assert (row.authored_ratio, row.authorship_confidence, row.authored_by_candidate) == (item.authored_ratio, item.authorship_confidence,
                                                                                         item.authored_by_candidate)
    # claim_text comes from the claim's verdict; r1 has no verdict in this test, so it is None (rendered "not measured")
    assert report.header.not_checkable == [{"claim_id": "r1", "claim_text": None, "kind": "role_or_team",
                                            "skip_reason": es_role.skip_reason, "source": "4B"}]
    assert report.header.analysed[0]["total_source_lines"] == v.report.repo_facts.total_source_lines
    assert report.header.grounding_failures == {"c_index": 0, "c_path": 0, "c_lines": 0, "c_authored": 0}
    text = render(report)
    assert "no score, no ranking, no hire/reject signal" in text and "src/cache.py" in text
    for field in report.model_json_schema()["$defs"]["ReportRow"]["properties"]:
        assert "score" not in field and "rank" not in field



# --- H0a: provenance + any content word -----------------------------------------------------------------------

def _item(path, rule="r_ownership", detail=None, kind="function"):
    from backend.contracts import EvidenceItem
    return EvidenceItem(repo_full_name="o/r", path=path, kind=kind, rule=rule, match_detail=detail, authored_by_candidate=False,
                        start_line=1, end_line=2)


@pytest.mark.parametrize("target,path,detail,expected_rule", [
    ("ledger reconciliation", "src/billing/ledger.py", "function reconcile_ledger", "whole_path_component:ledger"),
    ("payment reconciliation", "src/payment/reconcile.py", None, "whole_path_component:payment"),
    ("user authentication", "src/authentication/tokens.py", None, "whole_path_component:authentication"),
    ("data migration", "tools/run.py", "function data_migration", "whole_word_in_function:data"),
])
def test_any_content_word_may_match_and_the_word_is_recorded(target, path, detail, expected_rule):
    out = matrix.decide("supported", "ownership", _item(path, detail=detail), "specific_artifact", target)
    assert out.verdict == "contradicted" and out.scope_match_rule == f"r_ownership:{expected_rule}" and out.scope_match_score == 100.0


@pytest.mark.parametrize("target,path", [
    ("payment reconciliation", "src/reconciler.py"),      # the H0a docstring examples: no WHOLE word lines up,
    ("user authentication", "src/auth.py"),               # so the contradiction is missed (the safe direction)
    ("data migration", "scripts/migrate.py"),
])
def test_docstring_examples_are_still_blocked_under_whole_word_matching(target, path):
    out = matrix.decide("supported", "ownership", _item(path), "specific_artifact", target)
    assert out.verdict == "inconclusive" and out.scope_match_rule == "no_whole_word_match"


def test_ownership_contradiction_needs_r_ownership_provenance():
    for rule in ("r_import", "r_config", "r_dependency", "r_structure"):
        out = matrix.decide("supported", "ownership", _item("src/billing/ledger.py", rule=rule), "specific_artifact", "ledger reconciliation")
        assert out.verdict == "inconclusive" and "gate:provenance" in out.gates and out.reason == matrix.PROVENANCE_REASON
        assert out.scope_match_rule == "whole_path_component:ledger"  # the word match is still recorded
