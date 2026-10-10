"""Stage 1 (bullet-fragment splitter), deterministic. No API calls."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from backend.claims.split import MARKER, rebuild_text, split_lines, split_structure

DOCS = Path(__file__).resolve().parent / "fixtures" / "docs"


def lines_of(text: str):
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return out


def units(text: str):
    return split_lines(lines_of(text), text, section="EXPERIENCE", heading_path=["EXPERIENCE"])


def assert_traceable(text, us):
    for u in us:
        span = text[u.start:u.end]
        assert " ".join(MARKER.sub("", span.replace("\n", " "), count=1).split()) == u.text, (u.text, span)


def test_bullets_with_and_without_terminal_punctuation_are_separate_units():
    text = "• Built Redis caching layer serving 12k req/s\n• Migrated billing to Kubernetes.\n• Wrote the CI pipeline"
    us = units(text)
    assert [u.text for u in us] == ["Built Redis caching layer serving 12k req/s", "Migrated billing to Kubernetes.", "Wrote the CI pipeline"]
    assert {u.kind for u in us} == {"bullet"} and len({u.bullet_id for u in us}) == 3
    assert_traceable(text, us)


def test_two_unpunctuated_bullets_are_never_merged():
    text = "- Built the ledger service\n- Owned the on-call rotation"
    assert len(units(text)) == 2


def test_a_bullet_wrapping_to_two_lines_is_one_unit():
    text = "• Reduced p95 latency by 70% on the read path by moving hot keys to\n  an in-process LRU cache\n• Wrote load tests"
    us = units(text)
    assert [u.text for u in us][0] == "Reduced p95 latency by 70% on the read path by moving hot keys to an in-process LRU cache"
    assert len(us) == 2
    assert "\n" in text[us[0].start:us[0].end]  # the span covers both lines
    assert_traceable(text, us)


def test_a_bullet_with_two_sentences_gives_two_units_sharing_the_bullet():
    text = "• Built a Redis cache. It serves 12k req/s at peak.\n• Wrote docs"
    us = units(text)
    assert [u.text for u in us[:2]] == ["Built a Redis cache.", "It serves 12k req/s at peak."]
    assert us[0].bullet_id == us[1].bullet_id and us[2].bullet_id != us[0].bullet_id
    assert_traceable(text, us)


def test_a_sub_bullet_is_its_own_unit_with_its_parent_as_context():
    text = "• Led the payments migration\n  ◦ Moved 40 services to Kubernetes\n  ◦ Cut deploy time from 2 hours to 10 minutes\n• Wrote docs"
    us = units(text)
    assert [u.text for u in us] == ["Led the payments migration", "Moved 40 services to Kubernetes",
                                    "Cut deploy time from 2 hours to 10 minutes", "Wrote docs"]
    parent = us[0].bullet_id
    assert us[1].parent_bullet_id == parent and us[2].parent_bullet_id == parent and us[3].parent_bullet_id is None
    assert "Led the payments migration" in us[1].context()
    assert_traceable(text, us)


def test_abbreviations_and_decimals_do_not_split_sentences():
    us = units("B.Sc. Computer Science, University of Example. Graduated with a 3.8 GPA in Sep. 2018.")
    assert [u.text for u in us] == ["B.Sc. Computer Science, University of Example.", "Graduated with a 3.8 GPA in Sep. 2018."]


def test_numbered_list_items_are_bullets():
    us = units("1. Built the ledger\n2. Wrote the CI pipeline")
    assert [u.text for u in us] == ["Built the ledger", "Wrote the CI pipeline"] and {u.kind for u in us} == {"bullet"}


@pytest.mark.parametrize("name", ["resume_standard.pdf", "resume_contradictions.pdf", "docx_clean_resume.docx", "report_project.pdf"])
def test_real_fixture_units_trace_to_the_rebuilt_document_text(name):
    from backend.shield.analyze import analyze_document
    logging.disable(logging.CRITICAL)
    try:
        rep = analyze_document(DOCS / name)
    finally:
        logging.disable(logging.NOTSET)
    text, us, contact = split_structure(rep.structure)
    assert len(text) == rep.structure.sections[-1].char_end  # the rebuild matches the analyzer's own offsets
    for s in rep.structure.sections:
        if s.heading:
            assert text[s.char_start:s.char_start + len(s.heading)] == s.heading
    assert us and all(u.section is not None or u.heading_path == [] for u in us)
    for u in us:
        span = re.sub(r"\s+", " ", MARKER.sub("", text[u.start:u.end].replace("\n", " "), count=1)).strip()
        assert span == u.text, (name, u.unit_id)
    assert not any("@" in u.text for u in us)  # contact lines never become units


def test_role_line_glued_onto_a_bullet_is_split_out():
    text = "- Reduced p95 API latency by 38%. Engineer, Contoso Freight Jul 2018 - Feb 2021\n- Built a routing engine"
    us = units(text)
    assert us[1].text == "Engineer, Contoso Freight Jul 2018 - Feb 2021" and us[1].kind == "line" and us[1].bullet_id is None


def test_rebuild_is_the_analyzer_layout():
    from backend.contracts import ReferencesBlock, Section, StructureBlock
    block = StructureBlock(headings=[], links=[], numeric_claims=[], references=ReferencesBlock(section_found=False, entries=[], dois=[], arxiv_ids=[]),
                           sections=[Section(heading=None, page_start=1, page_end=1, char_start=0, char_end=5, text="Intro"),
                                     Section(heading="Skills", level=2, page_start=1, page_end=1, char_start=6, char_end=19, text="Python\nGo")])
    text, lines = rebuild_text(block)
    assert text == "Intro\nSkills\nPython\nGo" and text[6:12] == "Skills"
