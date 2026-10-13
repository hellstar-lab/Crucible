"""Stage 2 follow-ups: M2 on table rows, the row-context bleed, gerund fragments as non-assertions, role words with a
non-person agent. Offline (FakeLLM)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from backend.claims import modality, pipeline, routing, verbs
from backend.claims.split import split_lines
from test_claims_pipeline import J, fake  # noqa: F401  (the fixture)
from test_stacked_tables import PDF, needs_pdf

HEADER = ["Test Type", "Status (Pass/Fail)", "Notes"]
ROWS = [("Request handling test", "Pass", "Incoming portal requests route and ingest properly."),
        ("Queue management test", "Pass", "Queue buffers handle incoming items without dropping data."),
        ("Transaction test", "Pass", "Commit and rollback triggers operate as intended.")]


def table():
    lines = [NS(text=f"Test Type: {t}; Status (Pass/Fail): {s}; Notes: {n}", cells=[t, s, n], header=HEADER) for t, s, n in ROWS]
    return NS(row_lines=lines)


def row_units():
    text = "\n".join(rl.text for rl in table().row_lines)
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((pos, pos + len(ln), ln))
        pos += len(ln) + 1
    return split_lines(lines, text, section="Testing and Validation Status", heading_path=["Testing and Validation Status"]), text


# ---- A: M2 and a table row -----------------------------------------------------------------------------------------
TX = "Test Type: Transaction test; Status (Pass/Fail): Pass; Notes: Commit and rollback triggers operate as intended."


@pytest.mark.parametrize("claim", ["Test Type: Transaction test", "The transaction test passes.",
                                   "Commit and rollback triggers operate as intended."])
def test_m2_never_drops_a_claim_from_a_table_row(claim):
    """A reconstructed row has clause boundaries (";"), so it is never a fragment: the structural M2 (claim track
    close-out) needs no row special case. The old verb-lexicon M2 read "Test Type: ..." as verbless (first word "Test")."""
    assert not verbs.is_fragment(TX) and not verbs.fabricated_assertion(TX, claim)


def test_every_pass_row_is_one_verbatim_claim_with_no_model_call(fake):  # noqa: F811
    """Stage 2 close-out: a test-status row has exactly one possible verdict, so it never reaches a model. Every row,
    every run: the model path lost a different row in each of two live runs."""
    units, text = row_units()
    run = pipeline._run_units(units, text, {"selection": "nano", "disambiguation": "ultra", "decomposition": "nano"}, 1.0, None,
                              tables=[table()])
    assert fake.prompts == [] and run.calls == 0
    assert [(c.unit_id, c.text, c.test_status, c.decomposition_tier) for c in run.claims] == [
        (u.unit_id, " ".join(u.text.split()), "Pass", "deterministic") for u in units]
    assert run.excluded == []


def test_entity_check_ignores_sentence_punctuation():
    """General defect (Stage 2 close-out): "Pass." in a claim failed against "Pass;" in its source."""
    from backend.claims import support
    assert support.check("Status (Pass/Fail): Pass.", TX).fabricated_entities == []
    assert support.check("Deployed on Kubernetes.", TX).fabricated_entities == ["Kubernetes"]  # still a real fabrication
    assert support.entities("Used Node.js and ASP.NET.") == ["Node.js", "ASP.NET"]


# ---- B: a table row never sees its sibling rows ------------------------------------------------------------------------
def test_a_rows_context_holds_no_sibling_row(fake):  # noqa: F811
    """Rows that DO reach a model (a task table): no row's prompts carry another row's text."""
    rows = [("Built the request dispatcher", "Bansal, Abhinav"), ("Wrote the queue buffers that never drop items", "Bansal, Abhinav")]
    tbl = NS(row_lines=[NS(text=f"Task Completed: {t}; Team Member: {p}.", cells=[t, p], header=["Task Completed", "Team Member"])
                        for t, p in rows])
    text = "\n".join(rl.text for rl in tbl.row_lines)
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((pos, pos + len(ln), ln))
        pos += len(ln) + 1
    units = split_lines(lines, text, section="Tasks Completed", heading_path=["Tasks Completed"])
    assert rows[1][0] in " ".join(units[0].context_after)  # before isolation: the next row IS the first row's context
    pipeline.isolate_rows(units, [tbl])
    for u in units:
        assert not any("Task Completed:" in c for c in u.context_before + u.context_after), u.context()
    pipeline._run_units(units, text, {"selection": "nano", "disambiguation": "ultra", "decomposition": "nano"}, 1.0, None,
                        tables=[tbl], candidate="Bansal, Abhinav")
    first = [p for _task, _tier, p in fake.prompts if rows[0][0] in p]
    assert first and not any(rows[1][0] in p for p in first)  # the next row never reaches the first row's prompts


@needs_pdf
def test_on_the_phase_ii_pdf_the_request_handling_row_is_isolated():
    from backend.claims.split import split_structure
    from backend.shield.analyze import analyze_document
    doc = analyze_document(str(PDF))
    _text, units, _contact = split_structure(doc.structure)
    pipeline.isolate_rows(units, doc.structure.tables)
    u = next(u for u in units if "Request handling test" in u.text)
    assert "Queue buffers handle incoming items" not in u.context()


# ---- C: gerund fragments are non-assertions ------------------------------------------------------------------------------
@pytest.mark.parametrize("text,fragment", [
    ("Preventing CPU saturation.", True),
    ("4. Preventing Race Conditions and Overselling", True),
    ("Interfacing OS-level threading models smoothly with SQL commit/rollback workflows", True),
    ("Building a scheduler that processed 1M jobs", False),  # a finite verb in a relative clause: an assertion
    ("Moving forward, efforts center on building the adaptive research layer.", False),  # an introductory phrase + clause
    ("Monitoring dashboards were built", False),
    ("Deployed worker thread pools", False),
    ("Incoming portal requests route properly", False),  # "Incoming" is an adjective: the Request-handling row's claim
    ("Handling High-Concurrency Inflows", True),
])
def test_gerund_fragment(text, fragment):
    assert modality.gerund_fragment(text) is fragment


def test_a_gerund_fragment_claim_goes_to_modality_not_the_guard(fake):  # noqa: F811
    unit = "Finding the sweet spot between queue length and thread pool sizing proved difficult to prevent CPU saturation."
    fake.script[("claim_decomposition", unit)] = J("decomposed", claims=[{"text": "Preventing CPU saturation.", "qualifiers": []}])
    out = pipeline.extract_from_text(unit, headings=["Challenges"], decomposition_tier="nano")
    assert out.claims == [] and out.rejected_claims == []
    assert [(n.text, n.rule) for n in out.non_assertions] == [("Preventing CPU saturation.", "gerund_fragment")]


def test_why_m2_does_not_take_the_gerund_fragment():
    """The source is a 15-word sentence, not a fragment, so M2 (structural) does not judge it; the modality layer does."""
    src = "Finding the sweet spot between queue length and thread pool sizing proved difficult to prevent CPU saturation."
    assert not verbs.is_fragment(src)
    assert verbs.fabricated_assertion(src, "Preventing CPU saturation.") is False


# ---- D: a role word with a non-person agent is an artifact claim -------------------------------------------------------
@pytest.mark.parametrize("text,kind", [
    ("Incoming student requests pass through a semaphore-regulated buffer managed by the OS layer.", "artifact_exists"),
    ("Managed a team of 5 engineers", "role_or_team"),
    ("A service managed by the platform team", "role_or_team"),  # a team is a person agent
    ("Led a team of 4 engineers.", "role_or_team"),
])
def test_role_words_need_a_person_agent(text, kind):
    assert routing.route(text)["kind"] == kind
