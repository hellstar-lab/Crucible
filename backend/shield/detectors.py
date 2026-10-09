"""Independent attribute detectors. Each returns a reason string when it fires.

These work from text-layer attributes (plus the visibility measurement where
noted), so they corroborate render-and-diff and catch cases it can miss, such
as a hidden glyph whose box overlaps a neighbouring visible glyph.
"""

from __future__ import annotations

from . import config
from .extract import RGBA, BBox, CharInfo
from .visibility import RGB, CharMeasure

# PDF text render modes (ISO 32000 table 106).
_FILL_MODES = {0, 2, 4, 6}
_STROKE_MODES = {1, 2, 5, 6}
_INVISIBLE_MODES = {3, 7}  # 3 = neither fill nor stroke; 7 = clip only, never painted


def _used_colours(c: CharInfo) -> list[RGBA | None]:
    mode = c.render_mode if c.render_mode >= 0 else 0  # unknown: assume plain fill
    used: list[RGBA | None] = []
    if mode in _FILL_MODES:
        used.append(c.fill)
    if mode in _STROKE_MODES:
        used.append(c.stroke)
    return used


def invisible_render_mode(c: CharInfo) -> str | None:
    """Technique: text render mode 3 ("invisible") — the classic way to put an
    invisible OCR-style text layer in a PDF. Mode 7 (clip only) is also never
    painted, so it is caught too."""
    return "invisible_render_mode" if c.render_mode in _INVISIBLE_MODES else None


def transparent(c: CharInfo) -> str | None:
    """Technique: fully transparent text (alpha ~0). Only the colours the
    render mode actually paints are checked — outlined text with a
    transparent fill but an opaque stroke is still visible and must not fire."""
    used = _used_colours(c)
    if not used or any(col is None for col in used):
        return None
    if all(col[3] / 255 < config.MIN_OPACITY for col in used):  # type: ignore[index]
        return "transparent"
    return None


def sub_legible_font(c: CharInfo) -> str | None:
    """Technique: microscopic text (e.g. 0.5pt, 2pt) that is technically drawn
    but far too small for a human to read. Inclusive: fires when the rendered
    size is <= MIN_FONT_SIZE (within FONT_SIZE_EPSILON), so text set at exactly
    the threshold cannot slip under it."""
    if c.font_size <= config.MIN_FONT_SIZE + config.FONT_SIZE_EPSILON:
        return "sub_legible_font"
    return None


def off_page(c: CharInfo, page_box: BBox) -> str | None:
    """Technique: text positioned outside the visible page rectangle. Uses the
    box centre so a glyph that merely touches the margin does not fire."""
    left, bottom, right, top = c.bbox
    cx, cy = (left + right) / 2, (bottom + top) / 2
    pl, pb, pr, pt = page_box
    return None if (pl <= cx <= pr and pb <= cy <= pt) else "off_page"


def background_colour_match(c: CharInfo, background: RGB | None) -> str | None:
    """Technique: text coloured (nearly) the same as what is behind it, e.g.
    white text on a white page or off-white on white."""
    if background is None:
        return None
    used = [col for col in _used_colours(c) if col is not None and col[3] / 255 >= config.MIN_OPACITY]
    if not used:
        return None  # nothing painted; transparent / invisible_render_mode own this case
    if all(sum(abs(a - b) for a, b in zip(col[:3], background)) < config.BG_MATCH_DIST for col in used):
        return "background_colour_match"
    return None


def low_contrast(m: CharMeasure) -> str | None:
    """Technique: text that is drawn but too faint to read (very light grey on
    white, tiny alpha). Fires when the glyph changed pixels enough to count as
    drawn (>= DIFF_VISIBLE_LEVEL) but not enough to be legible (< CONTRAST_MIN)."""
    if m.diff_max is not None and config.DIFF_VISIBLE_LEVEL <= m.diff_max < config.CONTRAST_MIN:
        return "low_contrast"
    return None
