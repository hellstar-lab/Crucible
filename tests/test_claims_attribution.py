"""Stage 1 item 3: names_another_person. Not team mode: one candidate, rows naming someone else are excluded.
The page-3 fixture needs the OSDBMS phase-II PDF (not committed: personal data; see test_stacked_tables.py)."""

from __future__ import annotations

from types import SimpleNamespace as NS


from backend.claims import attribution, pipeline
from backend.claims.split import Unit
from test_claims_pipeline import fake  # noqa: F401  (the fixture)
from test_stacked_tables import PDF, needs_pdf

HEADER = ["Task Completed", "Team Member"]
ROWS = [("Built the request dispatcher and its buffer queue.", "Rathi, Kasak"),
        ("Deployed worker thread pools and semaphores.", "Bansal, Abhinav"),
        ("Wrote the transaction blocks.", "Shah, Yogesh"),
        ("Integrated the adaptive engine.", "All team members")]


def table():
    lines = [NS(text=f"Task Completed: {t.rstrip('.')}; Team Member: {p}.", cells=[t, p], header=HEADER) for t, p in ROWS]
    return NS(row_lines=lines, rows=[HEADER] + [list(r) for r in ROWS])


def test_same_person_ignores_order_and_a_middle_name():
    assert attribution.same_person("Bansal, Abhinav", "Abhinav Bansal")
    assert attribution.same_person("Rathi, Kasak", "Kasak R Rathi")
    assert not attribution.same_person("Bansal, Abhinav", "Rathi, Kasak")


def test_the_candidates_name_comes_from_the_cell_carrying_their_email():
    st = NS(tables=[NS(rows=[["Team member 2", "Bansal, Abhinav - 24021246 abhinavbansal2120@ gmail.com"]])], sections=[])
    assert attribution.candidate_name(st, ["abhinavbansal2120@gmail.com"]) == "Bansal, Abhinav"  # split by a line break
    assert attribution.candidate_name(st, ["someone@else.org"]) is None
    assert attribution.candidate_name(st, None) is None


def _run(candidate):
    t = table()
    units = [Unit(f"u{i}", rl.text, 0, len(rl.text), "Tasks Completed", ["Tasks Completed"], "sentence") for i, rl in enumerate(t.row_lines)]
    return pipeline._run_units(units, "\n".join(u.text for u in units), {"selection": "nano", "disambiguation": "ultra",
                                                                         "decomposition": "nano"}, 1.0, None, tables=[t],
                               candidate=candidate)


def test_rows_naming_another_person_are_excluded_before_any_model_call(fake):  # noqa: F811
    run = _run("Bansal, Abhinav")
    excluded = {e.unit_id: e for e in run.excluded}
    assert {k for k, e in excluded.items() if e.reason_code == "names_another_person"} == {"u0", "u2"}
    assert "'Rathi, Kasak'" in excluded["u0"].detail and "no retrieval, no verdict" in excluded["u0"].detail
    assert {c.unit_id for c in run.claims} == {"u1", "u3"}  # his row, and the whole team's
    assert {c.attributed_to for c in run.claims} == {"Bansal, Abhinav", "All team members"}  # the name stays on
    assert not any("Rathi" in p[2] or "Shah" in p[2] for p in fake.prompts)  # never sent to a model


def test_an_unresolved_candidate_verifies_no_named_row(fake):  # noqa: F811
    """Amendment 5: a document with no email beside the names. Never fall back to "it is the candidate's"."""
    st = NS(tables=[NS(rows=[["Team member 1", "Rathi, Kasak"], ["Team member 2", "Bansal, Abhinav"]])],
            sections=[NS(text="Team: Rathi, Kasak; Bansal, Abhinav")])
    assert attribution.candidate_name(st, ["abhinavbansal2120@gmail.com"]) is None  # no email beside any name
    run = _run(None)
    unresolved = {e.unit_id: e for e in run.excluded if e.reason_code == "names_unresolved"}
    assert set(unresolved) == {"u0", "u1", "u2"} and "'Bansal, Abhinav'" in unresolved["u1"].detail
    assert "not verified" in unresolved["u1"].detail
    assert [c.attributed_to for c in run.claims] == ["All team members"]  # a group includes the candidate
    assert not any("Rathi" in p[2] or "Bansal" in p[2] for p in fake.prompts)


def test_the_report_says_the_names_were_not_matched():
    import copy
    import json
    from pathlib import Path

    from backend.grounding.html import render_html
    d = json.loads((Path(__file__).parent / "fixtures" / "reports" / "degraded.json").read_text())
    d = copy.deepcopy(d)
    d["header"]["excluded_units"].append({"unit_id": "u1", "category": "names_unresolved", "text": "Task: Built X; Team Member: Bansal, Abhinav.",
                                          "section": "Tasks", "stage": "selection", "reason_code": "names_unresolved",
                                          "detail": "the document attributes this to 'Bansal, Abhinav' ... not verified", "source": "4A"})
    d["header"]["attribution"] = {"candidate_name": None, "note": "table rows name a person, but the candidate's name was not "
                                                                  "found in the document: none of them was verified"}
    page = render_html(d)
    assert "Named, but not matched to the candidate" in page and "none of them was verified" in page and "Bansal, Abhinav" in page


@needs_pdf
def test_phase_ii_page_3_verifies_only_abhinavs_row(fake):  # noqa: F811
    from backend.shield.analyze import analyze_document
    doc = analyze_document(str(PDF))
    out = pipeline.extract_claims(doc, decomposition_tier="nano", candidate_emails=["abhinavbansal2120@gmail.com"])
    assert out.candidate_name == "Bansal, Abhinav"
    page3 = [u for u in out.units if (u.source_section or "").startswith("Tasks Completed")]
    assert len(page3) == 4
    others = {e.unit_id for e in out.excluded if e.reason_code == "names_another_person"}
    texts = {u.unit_id: u.text for u in page3}
    assert {texts[i].split("Team Member: ")[1] for i in others if i in texts} == {"Rathi, Kasak.", "Shah, Yogesh.", "Dubey, Pragati."}
    verified = [c for c in out.claims if (c.source_section or "").startswith("Tasks Completed")]
    assert verified and {c.attributed_to for c in verified} == {"Bansal, Abhinav"}
