"""Shield: hidden-text and prompt-injection detection for resume PDFs.

Decides, for every character, whether a human could actually see it. Text a
machine can read but a human cannot is "hidden"; later stages consume only
``visible_text``.

Fully deterministic and offline: no network, no models. The result is two
states: ``clean``, or ``hidden_text_found`` with every fragment's recovered
text and the detector reasons that fired, for a human to judge. An LLM
severity classifier was evaluated and removed from this path; see
docs/classifier-experiment.md.

Public entry point: ``scan_pdf(path) -> ShieldResult``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from backend.contracts import HiddenFragment, ShieldSection

from . import config, detectors
from .errors import ShieldCorruptError, ShieldEncryptedError, ShieldError
from .extract import CharInfo, PlumberLayer, extract_page_chars
from .metadata_scan import scan_metadata
from .unicode_scan import UnicodeFinding, has_hidden_unicode, scan_text, strip_hidden_unicode
from .visibility import measure_page, not_drawn

__all__ = [
    "scan_pdf",
    "ShieldResult",
    "status_for",
    "PageStats",
    "ShieldError",
    "ShieldEncryptedError",
    "ShieldCorruptError",
]

log = logging.getLogger(__name__)

Status = Literal["clean", "hidden_text_found"]
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
class ShieldResult:
    visible_text: str
    hidden_fragments: list[HiddenFragment]
    status: Status
    page_stats: dict[int, PageStats] = field(default_factory=dict)

    def to_contract(self) -> ShieldSection:
        """The ``shield`` section of the Claims contract."""
        return ShieldSection(
            status=self.status,
            hidden_fragments=self.hidden_fragments,
            page_stats={p: asdict(s) for p, s in self.page_stats.items()},
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
    pdf: pdfium.PdfDocument, page_index: int, plumber: PlumberLayer
) -> tuple[list[HiddenFragment], str, PageStats]:
    page = pdf[page_index]
    textpage = page.get_textpage()
    try:
        page_no = page_index + 1
        chars = extract_page_chars(textpage, page_no)
        measure = measure_page(page, chars)
        page_box = tuple(page.get_cropbox())

        hidden: dict[int, list[str]] = {}
        for pos, c in enumerate(chars):
            if c.inkless:
                continue
            reasons = _char_reasons(c, measure.chars.get(pos), page_box)
            if reasons:
                hidden[c.index] = reasons

        fragments = _group_hidden(chars, hidden) + _unicode_fragments(chars, plumber, page_index)

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
        return fragments, visible, stats
    finally:
        textpage.close()
        page.close()


def _scan(path: Path) -> ShieldResult:
    pdf = _open(path)
    plumber = PlumberLayer(path)
    try:
        n_pages = len(pdf)
        if n_pages == 0:
            raise ShieldCorruptError(f"{path.name} has no pages")
        if n_pages > config.MAX_PAGES:
            raise ShieldError(f"{path.name} has {n_pages} pages; limit is {config.MAX_PAGES}")

        fragments: list[HiddenFragment] = []
        texts: list[str] = []
        stats: dict[int, PageStats] = {}
        for i in range(n_pages):
            page_frags, page_text, page_stats = _scan_page(pdf, i, plumber)
            fragments.extend(page_frags)
            texts.append(page_text)
            stats[page_stats.page] = page_stats

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

    return ShieldResult(
        visible_text="\n\n".join(texts),
        hidden_fragments=fragments,
        status=status_for(fragments),
        page_stats=stats,
    )


def scan_pdf(path: str | Path) -> ShieldResult:
    """Scan a resume PDF for hidden text. Deterministic and offline.

    Raises ShieldEncryptedError / ShieldCorruptError for unreadable input and
    ShieldError for anything else that goes wrong — never a raw crash.
    """
    path = Path(path)
    with _PDFIUM_LOCK:
        try:
            result = _scan(path)
        except ShieldError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert any parser failure into a ShieldError
            log.exception("shield scan failed for %s", path.name)
            raise ShieldError(f"failed to scan {path.name}: {exc}") from exc
    log.info(
        "shield scan %s: status=%s pages=%d fragments=%d",
        path.name, result.status, len(result.page_stats), len(result.hidden_fragments),
    )
    return result
