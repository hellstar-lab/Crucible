"""Document analyzer: one deterministic entry point from a PDF or DOCX to a ``DocumentReport``.

No model calls, no network. The document is hostile input: it is parsed, never executed, and no
URL, attachment or launch target in it is ever followed.

Failure policy ("fail toward suspicion"): a detector that raises is recorded in ``detector_errors``
and sets ``needs_review``; a truncated run (pages, bytes, seconds) is partial and sets ``truncated``
and ``needs_review``. Neither ever produces status "clean". Every field that was not measured is None.

OCR (``ocr=True``, CLI ``--ocr``) is off by default. Without it, image-only pages are listed in
``extraction_quality.ocr_required_pages`` and contribute no text; their text fields are None, not "".
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import date
from pathlib import Path

from backend.contracts import (DocumentInfo, DocumentReport, ExtractionQuality, HiddenFragment, SecurityFinding, StructureBlock)

from . import config, doc_kind, docx_scan, fonts, pdf_security, resume, structure
from . import ShieldEncryptedError, ShieldError, rollup_status, scan_pdf_detailed

log = logging.getLogger(__name__)
OCR_DPI_SCALE = 3.0


class _Errors:
    def __init__(self):
        self.items: list[dict] = []

    def run(self, detector: str, fn, default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one broken detector must not hide the others, and must not read as clean
            log.warning("detector %s failed: %s", detector, type(exc).__name__)
            self.items.append({"detector": detector, "error": f"{type(exc).__name__}: {exc}"[:200]})
            return default


def ocr_available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr_pages(path: Path, page_numbers: list[int]) -> dict[int, str]:
    """OCR the given 1-based pages (renders locally, runs the local tesseract binary). Lazy imports."""
    import pypdfium2 as pdfium
    import pytesseract

    out: dict[int, str] = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for n in page_numbers:
            page = pdf[n - 1]
            try:
                img = page.render(scale=OCR_DPI_SCALE).to_pil()
                out[n] = pytesseract.image_to_string(img)
            finally:
                page.close()
    finally:
        pdf.close()
    return out


def _merge_ocr(block: StructureBlock | None, ex: structure.Extracted | None, ocr_text: dict[int, str]):
    """Append OCR'd pages to the structure as plain paragraphs (no headings: OCR gives no font data)."""
    units = list(ex.units) if ex else []
    for page, text in sorted(ocr_text.items()):
        for para in [p for p in text.split("\n\n") if p.strip()]:
            clean, _ = structure.hygiene.clean(" ".join(para.split()))
            units.append(structure.Unit("paragraph", clean, page, 0.0, 0.0, []))
    sections, full_text = structure.build_sections(units)
    base = block or StructureBlock(headings=[], sections=[], tables=None, links=[], numeric_claims=[],
                                   references=structure.ReferencesBlock(section_found=False, entries=[], dois=[], arxiv_ids=[]))
    merged = base.model_copy(update={
        "sections": sections, "numeric_claims": structure.numeric_claims(units, sections),
        "references": structure.references_block(units, sections, full_text),
        "notes": base.notes + [f"OCR text from pages {sorted(ocr_text)} added as plain paragraphs"]})
    new_ex = structure.Extracted(merged, full_text, units, {}, ex.mojibake_fixed if ex else False, ex.text_pages if ex else [],
                                 ex.lines_by_page if ex else {}, ex.body_by_page if ex else {})
    return merged, new_ex


def _finish(info: DocumentInfo, status_inputs: tuple, **kw) -> DocumentReport:
    fragments, findings, needs_review = status_inputs
    return DocumentReport(document=info, status=rollup_status(fragments, findings, needs_review), needs_review=needs_review,
                          hidden_fragments=fragments, security_findings=findings, **kw)


def analyze_document(path: str | Path, *, ocr: bool = False, today: date | None = None,
                     max_seconds: float | None = None) -> DocumentReport:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _analyze_docx(path, today, max_seconds)
    return _analyze_pdf(path, ocr, today, max_seconds)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def _analyze_pdf(path: Path, ocr: bool, today: date | None, max_seconds: float | None) -> DocumentReport:
    errors = _Errors()
    warnings: list[str] = []
    info = DocumentInfo(path=path.name, format="pdf")
    try:
        shield = scan_pdf_detailed(path, max_seconds=max_seconds)
    except ShieldEncryptedError:
        info.encrypted = True
        return _finish(info, ([], [], True), extraction_quality=ExtractionQuality(),
                       warnings=["password-protected: no content could be examined"], detector_errors=[])
    if shield.truncated and not shield.pages:  # over the byte cap: nothing was examined
        info.truncated, info.truncation_reasons = True, shield.truncation_reasons
        return _finish(info, ([], [], True), extraction_quality=ExtractionQuality(),
                       warnings=list(shield.truncation_reasons), detector_errors=shield.detector_errors)

    deadline = time.monotonic() + (max_seconds if max_seconds is not None else config.MAX_SECONDS)
    findings: list[SecurityFinding] = list(shield.findings)
    fragments: list[HiddenFragment] = list(shield.hidden_fragments)
    errors.items += shield.detector_errors
    reader = errors.run("pypdf_open", lambda: pdf_security.open_reader(path))
    link_obs = errors.run("link_target_mismatch", lambda: pdf_security.link_observations(reader, shield.pages), []) if reader else []
    provenance = None
    if reader:
        findings += errors.run("link_target_mismatch", lambda: pdf_security.link_findings(link_obs), [])
        findings += errors.run("embedded_files", lambda: pdf_security.detect_embedded(reader), [])
        active = errors.run("active_content", lambda: pdf_security.detect_active(reader), ([], {}))
        findings += active[0]
        info.has_form = active[1].get("has_form")
        provenance = errors.run("provenance", lambda: pdf_security.extract_provenance(reader))
    findings += errors.run("revision_history", lambda: pdf_security.detect_revisions(path), [])

    per_page = [doc_kind.classify_page(pd) for pd in shield.pages]
    info.pages, info.pages_total = len(shield.pages), shield.pages_total
    info.per_page_kind = [p["kind"] for p in per_page]
    info.per_page_stats = per_page
    info.truncated, info.truncation_reasons = shield.truncated, shield.truncation_reasons
    ocr_required = [p["page"] for p in per_page if p["kind"] == "image_only"]
    no_text = [p["page"] for p in per_page if p["kind"] in ("image_only", "blank")]
    text_pages = {p["page"] for p in per_page if p["kind"] in ("text_pdf", "sparse", "slide_deck")}

    ex = errors.run("structure", lambda: structure.extract(str(path), shield.pages, link_obs, text_pages, deadline)) if text_pages else None
    if ex:
        findings += errors.run("font_anomaly", lambda: fonts.detect_font_anomaly(ex.lines_by_page, ex.body_by_page), [])
    block = ex.block if ex else None
    inventory = errors.run("font_inventory", lambda: fonts.font_inventory(reader, shield.pages)) if reader else None

    ocr_done: dict[int, str] = {}
    if ocr and ocr_required:
        if not ocr_available():
            warnings.append("--ocr requested but tesseract/pytesseract is not installed; image-only pages remain unread")
        else:
            ocr_done = errors.run("ocr", lambda: _ocr_pages(path, ocr_required), {}) or {}
            if ocr_done:
                block, ex = _merge_ocr(block, ex, ocr_done)
    ocr_left = [p for p in ocr_required if p not in ocr_done]
    if ocr_left:
        warnings.append(f"pages {ocr_left} are images without a text layer; OCR is required to read them (re-run with --ocr)")

    total = sum(1 for pd in shield.pages for c in pd.chars if not c.is_generated)
    unmapped = sum(1 for pd in shield.pages for c in pd.chars if c.unmapped and not c.is_generated)
    ratio = round(unmapped / total, 4) if total else None
    if ratio is not None and ratio > config.UNMAPPED_WARN_RATIO:
        warnings.append(f"{ratio:.0%} of characters have no Unicode mapping; extracted text is unreliable")
    quality = ExtractionQuality(chars_total=total if text_pages else None, chars_unmapped_ratio=ratio,
                                mojibake_fixed=ex.mojibake_fixed if ex else None, ocr_required_pages=ocr_left, pages_with_no_text=no_text)

    resume_block = None
    if ex and ex.units:
        conf, resume_block = errors.run("resume", lambda: resume.build_resume(ex, len(shield.pages), today=today), (None, None))
        info.resume_confidence = conf
    needs_review = bool(errors.items) or shield.truncated or shield.needs_review
    return _finish(info, (fragments, findings, needs_review), provenance=provenance, font_inventory=inventory, structure=block, resume=resume_block,
                   extraction_quality=quality, warnings=warnings, detector_errors=errors.items)


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def _analyze_docx(path: Path, today: date | None, max_seconds: float | None) -> DocumentReport:
    errors = _Errors()
    info = DocumentInfo(path=path.name, format="docx")
    res = errors.run("docx", lambda: docx_scan.scan_docx(path, max_seconds=max_seconds))
    if res is None:
        return _finish(info, ([], [], True), extraction_quality=ExtractionQuality(),
                       warnings=["the DOCX could not be parsed"], detector_errors=errors.items)
    info.truncated, info.truncation_reasons = res.truncated, res.truncation_reasons
    if res.extracted is None:
        return _finish(info, (res.fragments, res.findings, True), provenance=res.provenance, extraction_quality=ExtractionQuality(),
                       warnings=list(res.truncation_reasons), detector_errors=errors.items)
    ex = res.extracted
    conf, resume_block = errors.run("resume", lambda: resume.build_resume(ex, 1, today=today), (None, None))
    info.resume_confidence = conf
    quality = ExtractionQuality(chars_total=len(ex.full_text), chars_unmapped_ratio=None, mojibake_fixed=res.mojibake_fixed)
    needs_review = bool(errors.items) or res.truncated
    return _finish(info, (res.fragments, res.findings, needs_review), provenance=res.provenance, structure=ex.block, resume=resume_block,
                   extraction_quality=quality, detector_errors=errors.items)
