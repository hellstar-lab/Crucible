"""Phase 4M: project / entry headings are references (M1); the verbless-source guard (M2). Offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.claims import entries, pipeline, verbs
from backend.shield.analyze import analyze_document

from test_claims_pipeline import _UNIT, J, FakeLLM  # noqa: F401

DOCS = Path(__file__).resolve().parent / "fixtures" / "docs"


@pytest.fixture
def fake(monkeypatch):
    from backend import llm
    f = FakeLLM()
    monkeypatch.setattr(llm, "call", f)
    return f


# --- M2: the four measured cases -----------------------------------------------------------------------------------

@pytest.mark.parametrize("source,claim,fires", [
    ("Crucible, hiring-evidence system", "Built a Crucible, hiring-evidence system", True),
    ("Redis caching layer, 12k req/s", "Built a Redis caching layer serving 12k req/s", True),
    ("Python, Go, PostgreSQL", "Python", False),
    ("Led a team of 4 engineers", "Led a team of 4 engineers", False),
])
def test_verbless_source_guard_measured_cases(source, claim, fires):
    assert verbs.fabricated_assertion(source, claim) is fires


@pytest.mark.parametrize("source,claim", [
    ("Redesigning the checkout flow for mobile", "Redesigned the checkout flow"),  # stemmed: same verb, legitimate
    ("Senior Engineer, Northwind Payments", "Senior Engineer at Northwind Payments"),  # a title noun is not a verb
    ("Used Redis for caching", "Redis was used for caching"),  # paraphrase with a different verb form: never checked
])
def test_guard_does_not_fire_on_legitimate_rewrites(source, claim):
    assert not verbs.fabricated_assertion(source, claim)


def test_guard_excludes_the_unit_and_lists_it_as_could_not_process(fake):
    unit = "Redis caching layer, 12k req/s"
    fake.script[("claim_decomposition", unit)] = J("decomposed", claims=[{"text": "Built a Redis caching layer serving 12k req/s",
                                                                          "qualifiers": []}])
    out = pipeline.extract_from_text(unit, headings=["SUMMARY"], decomposition_tier="nano")
    assert out.claims == [] and out.counters["fabricated_assertion"] == 1
    [e] = out.excluded
    assert e.reason_code == "fabricated_assertion" and e.stage == "decomposition" and "Built a Redis" in e.detail
    from backend.grounding.report import NOT_A_CLAIM
    assert e.reason_code not in NOT_A_CLAIM  # the L5 split puts it under "could not process"


# --- M1: references ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,section,path,expected", [
    ("Crucible, hiring-evidence system", "entry_heading", "Crucible, hiring-evidence system", ["Name", "Projects"], "entry_heading"),
    ("Crucible, hiring-evidence system", "line", "Projects", ["Projects"], "entry_line"),
    ("B.Sc. Computer Science, University of Example - 2014 to 2018", "sentence", "Education", ["Education"], "entry_line"),
    ("Senior Engineer, Northwind Payments - Mar 2021 to Present", "line", "Experience", ["Experience"], None),  # a held role
    ("Built the cache client", "bullet", "Projects", ["Projects"], None),
    ("Reduced p95 latency by 70%.", "sentence", "Experience", ["Experience"], None),
    ("Crucible, hiring-evidence system", "line", "Summary", ["Summary"], None),  # not an entry section
])
def test_reference_kind(text, kind, section, path, expected):
    assert entries.reference_kind(text, kind, section, path) == expected


def test_crucible_resume_heading_is_a_reference_and_never_reaches_a_model(fake):
    out = pipeline.extract_claims(analyze_document(DOCS / "docx_crucible_resume.docx"), decomposition_tier="nano")
    assert [r.text for r in out.references] == ["Crucible, hiring-evidence system"]
    sent = [_UNIT.search(p).group(1) for _t, _tier, p in fake.prompts]
    assert "Crucible, hiring-evidence system" not in sent
    assert not any("Crucible, hiring-evidence system" in c.text for c in out.claims)


def test_report_lists_references_with_the_repos_they_name(fake):
    from backend.grounding.report import build_report, repos_named
    assert repos_named("Crucible, hiring-evidence system", ["hellstar-lab/Crucible", "o/other"]) == ["hellstar-lab/Crucible"]
    assert repos_named("Payments platform", ["hellstar-lab/Crucible"]) == []
    ext = pipeline.extract_claims(analyze_document(DOCS / "docx_crucible_resume.docx"), decomposition_tier="nano")
    rep = build_report(verdicts=[], evidence_sets=[], target_reports=[], extraction=ext)
    assert [r["text"] for r in rep.header.references] == ["Crucible, hiring-evidence system"]
    assert all(r["text"] not in [row.claim_text for row in rep.rows] for r in rep.header.references)


# --- 4G fold-in: verb_ambiguous unit-initial words count as verbless -----------------------------------------------

@pytest.mark.parametrize("source,claim", [("Release pipeline for mobile apps", "Built a release pipeline"),
                                          ("Design system for the checkout", "Designed the checkout design system")])
def test_verb_ambiguous_first_word_is_treated_as_verbless(source, claim):
    assert verbs.verb_ambiguous(source) and verbs.fabricated_assertion(source, claim)


def test_unambiguous_initial_verbs_still_count():
    assert not verbs.verb_ambiguous("Redesigned the checkout flow")
    assert not verbs.fabricated_assertion("Migrated the billing service to Go", "Migrated the billing service to Go")


def test_verb_ambiguous_drop_is_listed_as_could_not_process(fake):
    unit = "Release pipeline for mobile apps"
    fake.script[("claim_decomposition", unit)] = J("decomposed", claims=[{"text": "Built a release pipeline for mobile apps",
                                                                          "qualifiers": []}])
    out = pipeline.extract_from_text(unit, headings=["SUMMARY"], decomposition_tier="nano")
    [e] = out.excluded
    assert e.reason_code == "fabricated_assertion" and "verb_ambiguous" in e.detail
