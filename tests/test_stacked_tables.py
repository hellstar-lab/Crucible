"""2C: a ruled table with no internal row rules, and pdfTeX's justified word gaps.

The PDF tests run on the OSDBMS phase-II report when it is available (it carries four students' names, emails and
photos, so it is NOT committed as a fixture): set CRUCIBLE_PHASE2_PDF, or place "phase2report (4).pdf" in the repo
root. The geometry tests below need no PDF.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.shield.lines import Line, join_lines
from backend.shield.structure import PLUMBER_X_TOLERANCE, row_sentence, split_stacked

PDF = Path(os.environ.get("CRUCIBLE_PHASE2_PDF") or Path(__file__).resolve().parents[1] / "phase2report (4).pdf")
PAIRS = [("Built the foundational parallel request dispatcher coupled with a fixed-capacity buffer queue.", "Rathi, Kasak"),
         ("Deployed worker thread pools alongside synchronization semaphores to regulate execution threads.", "Bansal, Abhinav"),
         ("Wrote core transaction blocks, lock management routines, and checks for seat limits and duplicate entries.", "Shah, Yogesh"),
         ("Designed the MySQL relational schema for students, course catalogs, and logs, alongside registration stored "
          "procedures.", "Dubey, Pragati")]


def L(text, top, hyphen=False, h=10.7):
    return Line(page=3, text=text, x0=0, x1=100, top=top, bottom=top + h, size=12, bold_fraction=0, segments=[text], runs=[],
                n_chars=len(text), hyphen_end=hyphen)


def test_items_split_by_vertical_gap_and_values_go_to_the_item_whose_span_contains_them():
    # measured on page 3: 14.5 pt inside an item, 28.9 pt between items; items of 2 and 3 lines
    left, right, top = [], [], 100.0
    for k, n in enumerate((2, 2, 3, 3)):
        item = [L(f"item{k} line{i}", top + 14.5 * i) for i in range(n)]
        left += item
        mid = item[len(item) // 2]  # LaTeX centres the name vertically: beside the MIDDLE line of a 3-line item
        right.append(L(f"name{k}", mid.top if n == 3 else (item[0].top + item[-1].top) / 2))
        top = item[-1].top + 28.9
    rows = split_stacked([left, right])
    assert [[ln.text for ln in r[1]] for r in rows] == [["name0"], ["name1"], ["name2"], ["name3"]]
    assert [len(r[0]) for r in rows] == [2, 2, 3, 3]
    # a same-top matcher would give name2 to nothing (no item starts at its top): the span rule must not
    assert all(r[0][0].top <= r[1][0].top <= r[0][-1].bottom for r in rows)


def test_a_row_with_one_item_per_cell_is_not_split():
    assert split_stacked([[L("only", 0), L("still the same item", 14.5)], [L("Pass", 0)]]) is None
    assert split_stacked([[L("one line", 0)], [L("x", 0)]]) is None


def test_value_lines_below_a_one_line_item_stay_with_it():
    # page 6: a one-line test name, a two-line note that starts level with it
    left = [L("Request handling test", 0), L("Queue management test", 43.5)]
    notes = [L("Incoming portal requests", 0), L("route and ingest properly.", 14.5), L("Queue buffers handle", 43.5)]
    rows = split_stacked([left, notes])
    assert [[ln.text for ln in r[1]] for r in rows] == [["Incoming portal requests", "route and ingest properly."],
                                                         ["Queue buffers handle"]]


def test_line_end_hyphens_join_words_and_keep_compounds():
    assert join_lines([L("pools alongside synchro", 0, hyphen=True), L("nization semaphores", 14.5)]) == \
        "pools alongside synchronization semaphores"
    assert join_lines([L("coordination of OS", 0, hyphen=True), L("DBMS layers", 14.5)]) == "coordination of OS-DBMS layers"
    assert join_lines([L("no hyphen", 0), L("here", 14.5)]) == "no hyphen here"


def test_row_sentence_keeps_the_attribution():
    assert row_sentence(["Task Completed", "Team Member"], ["Built the queue.", "Rathi, Kasak"]) == \
        "Task Completed: Built the queue; Team Member: Rathi, Kasak."
    assert row_sentence(None, ["a", "b"]) == "a; b."


# ---- on the real PDF -----------------------------------------------------------------------------------------------
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="the OSDBMS phase-II PDF is not present (not committed: personal data)")


@pytest.fixture(scope="module")
def doc():
    from backend.shield.analyze import analyze_document
    return analyze_document(str(PDF))


@needs_pdf
def test_the_abstracts_first_line_comes_back_with_spaces(doc):
    import pdfplumber
    with pdfplumber.open(str(PDF)) as pdf:
        p2 = pdf.pages[1]
        merged = next(ln for ln in p2.extract_text().splitlines() if ln.startswith("Our"))
        fixed = next(ln for ln in p2.extract_text(x_tolerance=PLUMBER_X_TOLERANCE).splitlines() if ln.startswith("Our"))
    assert merged.startswith("Oursystem,ConcurrentReg,handles")  # pdfplumber's default (3) on pdfTeX justification
    assert fixed.startswith("Our system, ConcurrentReg, handles the messy performance spikes")
    abstract = next(s for s in doc.structure.sections if s.heading.startswith("Project Abstract"))
    assert abstract.text.startswith("Our system, ConcurrentReg, handles the messy performance spikes that occur")
    longest = max((w for s in doc.structure.sections for w in s.text.split() if "://" not in w), key=len)
    assert len(longest) < 40, longest


@needs_pdf
def test_page_3_yields_all_four_task_member_pairs(doc):
    t = next(t for t in doc.structure.tables if t.page == 3)
    assert t.rows[0] == ["Task Completed", "Team Member"]
    assert [tuple(r) for r in t.rows[1:]] == PAIRS
    sec = next(s for s in doc.structure.sections if s.heading.startswith("Tasks Completed"))
    for task, who in PAIRS:
        assert f"Task Completed: {task.rstrip('.')}; Team Member: {who}." in sec.text
    assert "Rathi, Kasak coupled" not in sec.text  # the interleaved column text is gone


@needs_pdf
def test_pages_4_and_6_split_the_same_way(doc):
    p4 = next(t for t in doc.structure.tables if t.page == 4)
    assert [r[1] for r in p4.rows[1:]] == ["Rathi, Kasak", "Bansal, Abhinav", "Shah, Yogesh", "Dubey, Pragati", "All team members"]
    p6 = next(t for t in doc.structure.tables if t.page == 6)
    assert p6.rows[4] == ["High load test", "Pending", "Burst and heavy stress simulations remain scheduled."]
    assert p6.rows[5] == ["concurrent request test", "In progress", "Multi-client simulation testing is actively running."]
