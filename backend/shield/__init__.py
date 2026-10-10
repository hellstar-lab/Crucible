"""Shield: hidden-text and prompt-injection detection for resume PDFs.

Decides, for every character, whether a human could actually see it. Text a
machine can read but a human cannot is "hidden"; later stages consume only
``visible_text``.

Fully deterministic and offline: no network, no models. The status is a
roll-up of findings, never a verdict:

  clean              nothing found
  suspicious         a finding of severity medium/high that is not hidden text
                     (e.g. text outside the page boxes, a mismatched link), or the
                     scan was partial / a detector errored (fail toward suspicion)
  hidden_text_found  hidden text found; each fragment's recovered text and the
                     detector reasons that fired are returned for a human to judge

An LLM severity classifier was evaluated and removed from this path; see
docs/classifier-experiment.md. Full document analysis (links, attachments,
structure, resume layer, DOCX) lives in ``backend.shield.analyze``.

Public entry points: ``scan_pdf(path) -> ShieldResult`` and, for the analyzer,
``scan_pdf_detailed(path)`` which also keeps per-page character data.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from backend.contracts import HiddenFragment, SecurityFinding, ShieldSection

from . import config, detectors
from .errors import ShieldCorruptError, ShieldEncryptedError, ShieldError
from .extract import CharInfo, PlumberLayer, count_page_objects, extract_page_chars
from .geometry import Box, PageBoxes, detect_offpage, page_boxes
from .metadata_scan import scan_metadata
from .unicode_scan import UnicodeFinding, has_hidden_unicode, scan_text, strip_hidden_unicode
from .visibility import measure_page, not_drawn

__all__ = [
    "scan_pdf",
    "scan_pdf_detailed",
    "ShieldResult",
    "PageData",
    "status_for",
    "rollup_status",
    "PageStats",
    "ShieldError",
    "ShieldEncryptedError",
    "ShieldCorruptError",
]

log = logging.getLogger(__name__)
# pdfminer (inside pdfplumber) logs raw content-stream tokens at DEBUG, i.e. the document's text. Keep it out of any log.
logging.getLogger("pdfminer").setLevel(logging.WARNING)

Status = Literal["clean", "suspicious", "hidden_text_found"]
_RENDER_REASONS = {"not_drawn", "low_contrast"}
_PDFIUM_ERR_PASSWORD = 4

# pdfium is not thread-safe; serialise every document operation.
_PDFIUM_LOCK = threading.Lock()


@dataclass
class PageStats:
    page: int
    chars: int
    inked_chars: int
    hidden_chars: int
    fragments: int
    render_ms: float


@dataclass
class PageData:
    """Per-page material kept for the document analyzer (not part of the contract)."""

    page: int
    width: float  # effective page size in points (CropBox ∩ MediaBox, after /Rotate)
    height: float
    boxes: PageBoxes
    rotation: int
    chars: list[CharInfo]  # every char, in content-stream order
    hidden: frozenset[int]  # CharInfo.index values judged hidden by the Phase 2A detectors
    unmapped: int  # chars pdfium could not map to Unicode
    images: int = 0  # image objects painted on the page
    paths: int = 0  # path objects (rules, rectangles) painted on the page


@dataclass
class ShieldResult:
    visible_text: str
    hidden_fragments: list[HiddenFragment]
    status: Status
    page_stats: dict[int, PageStats] = field(default_factory=dict)
    findings: list[SecurityFinding] = field(default_factory=list)  # geometry detectors (Phase 2C)
    needs_review: bool = False  # a detector errored or the scan was partial: never "clean"
    detector_errors: list[dict] = field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    pages_total: int = 0
    pages: list[PageData] = field(default_factory=list)  # only from scan_pdf_detailed

    def to_contract(self) -> ShieldSection:
        """The ``shield`` section of the Claims contract."""
        return ShieldSection(
            status=self.status,
            hidden_fragments=self.hidden_fragments,
            page_stats={p: asdict(s) for p, s in self.page_stats.items()},
            findings=self.findings,
            needs_review=self.needs_review,
            truncated=self.truncated,
        )


# --------------------------------------------------------------------------
# per-character decision
# --------------------------------------------------------------------------


def _char_reasons(c: CharInfo, m, page_box) -> list[str]:
    reasons = [
        detectors.invisible_render_mode(c),
        detectors.transparent(c),
        detectors.sub_legible_font(c),
        detectors.off_page(c, page_box),
    ]
    if m is not None:
        reasons += [
            not_drawn(m),
            detectors.low_contrast(m),
            detectors.background_colour_match(c, m.background),
        ]
    return [r for r in reasons if r]


# --------------------------------------------------------------------------
# fragment grouping
# --------------------------------------------------------------------------


def _center_y(c: CharInfo) -> float:
    return (c.bbox[1] + c.bbox[3]) / 2


def _same_run(prev: CharInfo, nxt: CharInfo) -> bool:
    size = max(prev.font_size, nxt.font_size, config.FRAGMENT_MIN_LINE_HEIGHT)
    return abs(_center_y(prev) - _center_y(nxt)) <= config.FRAGMENT_LINE_GAP * size


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    boxes = [b for b in boxes if b[2] > b[0] or b[3] > b[1]]
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    )


def _make_visual_fragment(run: list[CharInfo], reasons_by_idx: dict[int, list[str]]) -> HiddenFragment:
    reasons: list[str] = []
    for c in run:
        for r in reasons_by_idx.get(c.index, []):
            if r not in reasons:
                reasons.append(r)
    methods = sorted({"render_diff" if r in _RENDER_REASONS else "attribute" for r in reasons})
    return HiddenFragment(
        text=" ".join("".join(c.text for c in run).split()),
        page=run[0].page,
        bbox=_union([c.bbox for c in run if not c.inkless]),
        reasons=reasons,
        detection_method="+".join(methods),
    )


def _group_hidden(chars: list[CharInfo], hidden: dict[int, list[str]]) -> list[HiddenFragment]:
    """Join adjacent hidden chars into fragments. Whitespace between hidden
    chars is carried along; a visible inked char or a large vertical jump
    ends the fragment."""
    frags: list[HiddenFragment] = []
    run: list[CharInfo] = []
    bridge: list[CharInfo] = []
    for c in chars:
        if c.index in hidden:
            if run and not _same_run(run[-1], c):
                frags.append(_make_visual_fragment(run, hidden))
                run = []
            run.extend(bridge if run else [])
            run.append(c)
            bridge = []
        elif c.inkless and run:
            bridge.append(c)
        else:
            if run:
                frags.append(_make_visual_fragment(run, hidden))
            run, bridge = [], []
    if run:
        frags.append(_make_visual_fragment(run, hidden))
    return frags


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def rollup_status(fragments: list[HiddenFragment], findings: list[SecurityFinding], needs_review: bool = False) -> Status:
    """hidden_text_found if any hidden fragment; else suspicious if any finding of
    severity medium/high or the result needs review; else clean."""
    if fragments:
        return "hidden_text_found"
    if needs_review or any(f.severity in ("medium", "high") for f in findings):
        return "suspicious"
    return "clean"


def status_for(fragments: list[HiddenFragment]) -> Status:
    """Two states, decided only by deterministic detection: any hidden
    fragment -> hidden_text_found, otherwise clean. Severity is left to the
    human reviewer, who sees the recovered text and the detector reasons."""
    return "hidden_text_found" if fragments else "clean"


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _open(path: Path) -> pdfium.PdfDocument:
    if not path.is_file():
        raise ShieldError(f"PDF not found: {path}")
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == _PDFIUM_ERR_PASSWORD:
            raise ShieldEncryptedError(f"{path.name} is password-protected") from exc
        raise ShieldCorruptError(f"{path.name} is not a readable PDF: {exc}") from exc


def _unicode_fragments(
    chars: list[CharInfo], plumber: PlumberLayer, page_index: int
) -> list[HiddenFragment]:
    """Unicode findings for a page, from pdfium's layer and — when that layer
    shows any hidden code point — pdfplumber's complete layer too."""
    page_no = page_index + 1
    pdfium_text = "".join(c.text for c in chars)
    layers: list[tuple[str, list]] = [(pdfium_text, [c.bbox for c in chars])]
    if has_hidden_unicode(pdfium_text):
        alt = plumber.page_text(page_index)
        if alt is not None:
            layers.insert(0, alt)

    seen: set[tuple[str, str]] = set()
    out: list[HiddenFragment] = []
    for text, boxes in layers:
        f: UnicodeFinding
        for f in scan_text(text):
            key = (f.kind, f.text)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                HiddenFragment(
                    text=f.text,
                    page=page_no,
                    bbox=_union(boxes[f.start:f.end]),
                    reasons=[f.kind],
                    detection_method="unicode",
                )
            )
    return out


def _scan_page(
    pdf: pdfium.PdfDocument, page_index: int, plumber: PlumberLayer, with_fonts: bool = False, keep: bool = False,
    errors: list[dict] | None = None,
) -> tuple[list[HiddenFragment], str, PageStats, list[SecurityFinding], PageData | None]:
    page = pdf[page_index]
    textpage = page.get_textpage()
    errors = errors if errors is not None else []
    try:
        page_no = page_index + 1
        chars = extract_page_chars(textpage, page_no, with_fonts)
        try:
            measure = measure_page(page, chars)
        except Exception as exc:  # noqa: BLE001 - fail toward suspicion, never crash the scan
            log.warning("render-diff failed on page %d: %s", page_no, exc)
            errors.append({"detector": "render_diff", "page": page_no, "error": f"{type(exc).__name__}: {exc}"[:200]})
            from .visibility import PageMeasure

            measure = PageMeasure({}, 0.0)
        # The visible region is CropBox ∩ MediaBox. pdfium's get_cropbox() is the RAW declared
        # box, so an oversize CropBox used to make off-page text look on-page.
        pb = page_boxes(page.get_mediabox(), page.get_cropbox())
        page_box = pb.effective

        hidden: dict[int, list[str]] = {}
        for pos, c in enumerate(chars):
            if c.inkless:
                continue
            reasons = _char_reasons(c, measure.chars.get(pos), page_box)
            if reasons:
                hidden[c.index] = reasons

        fragments = _group_hidden(chars, hidden) + _unicode_fragments(chars, plumber, page_index)
        findings: list[SecurityFinding] = []
        try:
            findings = detect_offpage(page_no, chars, pb)
        except Exception as exc:  # noqa: BLE001
            errors.append({"detector": "offpage_text", "page": page_no, "error": f"{type(exc).__name__}: {exc}"[:200]})

        visible = "".join(c.text for c in chars if c.index not in hidden)
        visible = strip_hidden_unicode(visible).replace("\r\n", "\n")

        stats = PageStats(
            page=page_no,
            chars=len(chars),
            inked_chars=sum(1 for c in chars if not c.inkless),
            hidden_chars=len(hidden),
            fragments=len(fragments),
            render_ms=round(measure.render_ms, 1),
        )
        data = None
        if keep:
            w, h = page.get_size()
            images, paths = count_page_objects(page)
            data = PageData(page_no, float(w), float(h), pb, int(page.get_rotation()), chars, frozenset(hidden),
                            sum(1 for c in chars if c.unmapped), images, paths)
        return fragments, visible, stats, findings, data
    finally:
        textpage.close()
        page.close()


def _scan(path: Path, keep: bool = False, with_fonts: bool = False, max_seconds: float | None = None) -> ShieldResult:
    size_mb = path.stat().st_size / 1_048_576 if path.is_file() else 0
    if size_mb > config.MAX_FILE_MB:  # hostile-size input: examine nothing, and say so
        reason = f"file is {size_mb:.0f} MB, over the {config.MAX_FILE_MB} MB cap; no page was examined"
        return ShieldResult("", [], "suspicious", needs_review=True, truncated=True, truncation_reasons=[reason])
    deadline = time.monotonic() + (max_seconds if max_seconds is not None else config.MAX_SECONDS)
    pdf = _open(path)
    plumber = PlumberLayer(path)
    try:
        n_pages = len(pdf)
        if n_pages == 0:
            raise ShieldCorruptError(f"{path.name} has no pages")
        reasons: list[str] = []
        limit = min(n_pages, config.MAX_PAGES)
        if n_pages > config.MAX_PAGES:
            reasons.append(f"{n_pages} pages, analysed the first {config.MAX_PAGES} (cap)")

        fragments: list[HiddenFragment] = []
        findings: list[SecurityFinding] = []
        errors: list[dict] = []
        texts: list[str] = []
        stats: dict[int, PageStats] = {}
        pages: list[PageData] = []
        for i in range(limit):
            if time.monotonic() > deadline:
                reasons.append(f"time budget of {max_seconds or config.MAX_SECONDS:.0f}s reached after {i} of {limit} pages")
                break
            page_frags, page_text, page_stats, page_findings, data = _scan_page(pdf, i, plumber, with_fonts, keep, errors)
            fragments.extend(page_frags)
            findings.extend(page_findings)
            texts.append(page_text)
            stats[page_stats.page] = page_stats
            if data is not None:
                pages.append(data)

        for m in scan_metadata(pdf, path):
            fragments.append(
                HiddenFragment(
                    text=m.value,
                    page=0,
                    bbox=(0.0, 0.0, 0.0, 0.0),
                    reasons=["overlong_metadata", f"field:{m.field}"],
                    detection_method="metadata",
                )
            )
    finally:
        plumber.close()
        pdf.close()

    needs_review = bool(errors) or bool(reasons)  # a partial or errored scan is never "clean"
    return ShieldResult(
        visible_text="\n\n".join(texts),
        hidden_fragments=fragments,
        status=rollup_status(fragments, findings, needs_review),
        page_stats=stats,
        findings=findings,
        needs_review=needs_review,
        detector_errors=errors,
        truncated=bool(reasons),
        truncation_reasons=reasons,
        pages_total=n_pages,
        pages=pages,
    )


def _guarded(path: Path, **kw) -> ShieldResult:
    path = Path(path)
    with _PDFIUM_LOCK:
        try:
            result = _scan(path, **kw)
        except ShieldError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert any parser failure into a ShieldError
            log.exception("shield scan failed for %s", path.name)
            raise ShieldError(f"failed to scan {path.name}: {exc}") from exc
    log.info(
        "shield scan %s: status=%s pages=%d fragments=%d findings=%d truncated=%s",
        path.name, result.status, len(result.page_stats), len(result.hidden_fragments), len(result.findings),
        result.truncated,
    )
    return result


def scan_pdf_detailed(path: str | Path, *, with_fonts: bool = True, max_seconds: float | None = None) -> ShieldResult:
    """Like ``scan_pdf`` but keeps per-page character data (``result.pages``) for the analyzer."""
    return _guarded(Path(path), keep=True, with_fonts=with_fonts, max_seconds=max_seconds)


def scan_pdf(path: str | Path) -> ShieldResult:
    """Scan a resume PDF for hidden text. Deterministic and offline.

    Raises ShieldEncryptedError / ShieldCorruptError for unreadable input and
    ShieldError for anything else that goes wrong — never a raw crash. Over a cap
    (pages, bytes, seconds) the result is partial with ``truncated=True`` and
    ``needs_review=True``, never a silent half-answer.
    """
    return _guarded(Path(path))
