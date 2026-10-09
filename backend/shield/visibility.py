"""Render-and-diff visibility measurement — the core hidden-text detector.

Technique caught: any text that is present in the text layer but does not
change a single rendered pixel. This covers white-on-white text, text drawn
underneath an opaque shape, text clipped away, text in render mode 3/7, and
text positioned off the visible page — without needing to know which trick
was used.

Method:
  1. Render the page as-is.
  2. Switch every text object (recursing into form XObjects) to render mode 3
     so no glyph is painted, render again, then restore the original modes.
     Switching modes instead of deleting objects keeps the page structure
     intact and reaches text nested inside forms.
  3. Diff the renders. For each character box take the MAXIMUM pixel diff —
     a glyph covers only part of its box, so a mean would wash the signal out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageChops, ImageStat

from . import config
from .extract import CharInfo

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]


@dataclass
class CharMeasure:
    diff_max: int | None  # None when the box is degenerate or entirely off the render
    background: RGB | None  # median colour behind the glyph, from the text-removed render


@dataclass
class PageMeasure:
    chars: dict[int, CharMeasure]  # keyed by position in the page's CharInfo list
    render_ms: float


def _text_objects(page: pdfium.PdfPage) -> list:
    """Every text object on the page, including those nested in form XObjects."""
    found = []

    def walk(count: int, get) -> None:
        for i in range(count):
            obj = get(i)
            kind = pdfium_c.FPDFPageObj_GetType(obj)
            if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                found.append(obj)
            elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                walk(pdfium_c.FPDFFormObj_CountObjects(obj),
                     lambda j, form=obj: pdfium_c.FPDFFormObj_GetObject(form, j))

    walk(pdfium_c.FPDFPage_CountObjects(page.raw), lambda i: pdfium_c.FPDFPage_GetObject(page.raw, i))
    return found


def _render(page: pdfium.PdfPage) -> tuple[Image.Image, pdfium.PdfPosConv]:
    bitmap = page.render(scale=config.RENDER_SCALE)
    return bitmap.to_pil().convert("RGB"), bitmap.get_posconv(page)


def _render_without_text(page: pdfium.PdfPage) -> Image.Image:
    objs = _text_objects(page)
    saved = [pdfium_c.FPDFTextObj_GetTextRenderMode(o) for o in objs]
    try:
        for o in objs:
            pdfium_c.FPDFTextObj_SetTextRenderMode(o, pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE)
        img, _ = _render(page)
    finally:
        for o, mode in zip(objs, saved):
            pdfium_c.FPDFTextObj_SetTextRenderMode(o, mode)
    return img


def _pixel_box(c: CharInfo, posconv: pdfium.PdfPosConv, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    left, bottom, right, top = c.bbox
    if right - left <= 0 or top - bottom <= 0:
        return None
    x0, y0 = posconv.to_bitmap(left, top)
    x1, y1 = posconv.to_bitmap(right, bottom)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    inset = config.DIFF_BOX_INSET_PX
    if x1 - x0 > 2 * inset + 1 and y1 - y0 > 2 * inset + 1:
        x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    w, h = size
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def measure_page(page: pdfium.PdfPage, chars: list[CharInfo]) -> PageMeasure:
    """Measure, for every inked character, how much rendering it changed the page."""
    start = time.perf_counter()
    targets = [i for i, c in enumerate(chars) if not c.inkless]
    if not targets:
        return PageMeasure({}, (time.perf_counter() - start) * 1000)

    with_text, posconv = _render(page)
    without_text = _render_without_text(page)

    # Per-pixel max across channels rather than luminance, so a change in
    # any one channel (e.g. pure blue on black) is not under-weighted.
    r, g, b = ImageChops.difference(with_text, without_text).split()
    diff = ImageChops.lighter(ImageChops.lighter(r, g), b)

    out: dict[int, CharMeasure] = {}
    for i in targets:
        box = _pixel_box(chars[i], posconv, diff.size)
        if box is None:
            out[i] = CharMeasure(None, None)
            continue
        diff_max = diff.crop(box).getextrema()[1]
        bg = tuple(int(v) for v in ImageStat.Stat(without_text.crop(box)).median)
        out[i] = CharMeasure(diff_max, bg)  # type: ignore[arg-type]

    elapsed = (time.perf_counter() - start) * 1000
    log.debug("page %d: measured %d chars in %.1f ms", chars[0].page, len(targets), elapsed)
    return PageMeasure(out, elapsed)


def not_drawn(m: CharMeasure) -> str | None:
    """Technique: any trick that leaves the glyph unpainted (covered, clipped,
    same colour as background, invisible mode). Fires when rendering the
    character changed no pixel in its box by DIFF_VISIBLE_LEVEL or more."""
    if m.diff_max is not None and m.diff_max < config.DIFF_VISIBLE_LEVEL:
        return "not_drawn"
    return None
