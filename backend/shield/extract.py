"""Per-character extraction from a PDF using pypdfium2's raw pdfium bindings.

Coordinates are PDF user-space points with the origin at the bottom-left.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

log = logging.getLogger(__name__)

RGBA = tuple[int, int, int, int]
BBox = tuple[float, float, float, float]  # (left, bottom, right, top)

# Characters that never draw ink by design. They are judged by unicode_scan
# (with its run-length rules), not by the visibility checks.
FORMAT_CHARS = frozenset(
    [0x00AD, 0x034F, 0x180E, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061,
     0x2062, 0x2063, 0x2064, 0xFEFF, 0x3164, 0xFFA0, 0x115F, 0x1160]
    + list(range(0x202A, 0x202F))
    + list(range(0x2066, 0x2070))
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0000, 0xE0080))
    + list(range(0xE0100, 0xE01F0))
)


@dataclass(slots=True)
class CharInfo:
    index: int  # index within the page's text page
    text: str
    page: int  # 1-based
    bbox: BBox  # user space, unrotated, bottom-up: the same system as the page boxes
    font_size: float  # effective rendered size in points
    fill: RGBA | None
    stroke: RGBA | None
    render_mode: int  # -1 when pdfium cannot resolve the owning text object
    is_generated: bool  # pdfium-inserted space / line break, not in the content stream
    font_name: str = ""  # PostScript name incl. any ABCDEF+ subset prefix; "" unless fonts were requested
    unmapped: bool = False  # pdfium has no Unicode mapping for the glyph (text is unreliable)
    rotated: bool = False  # glyph is not horizontal (margin stamps such as the arXiv sidebar)

    @property
    def inkless(self) -> bool:
        """True for chars that never draw a glyph (whitespace, format chars)."""
        return (
            self.is_generated
            or not self.text
            or self.text.isspace()
            or ord(self.text[0]) in FORMAT_CHARS
            or ord(self.text[0]) < 0x20
        )


def _color(fn, textpage, i: int) -> RGBA | None:
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    ok = fn(textpage, i, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a))
    return (r.value, g.value, b.value, a.value) if ok else None


def _effective_font_size(tp, i: int, matrix) -> float:
    """Rendered size in points. FPDFText_GetFontSize returns the Tf operand
    only; many generators (e.g. Quartz) write ``1 Tf`` and scale with the
    text matrix, so multiply by the scale of the char's effective matrix
    (text matrix x CTM). This also catches text shrunk via the matrix."""
    size = float(pdfium_c.FPDFText_GetFontSize(tp, i))
    if pdfium_c.FPDFText_GetMatrix(tp, i, ctypes.byref(matrix)):
        size *= abs(matrix.a * matrix.d - matrix.b * matrix.c) ** 0.5
    return size


def _font_name(tp, i: int) -> str:
    flags = ctypes.c_int(0)
    n = pdfium_c.FPDFText_GetFontInfo(tp, i, None, 0, ctypes.byref(flags))
    if n <= 0:
        return ""
    buf = ctypes.create_string_buffer(n)
    pdfium_c.FPDFText_GetFontInfo(tp, i, buf, n, ctypes.byref(flags))
    return buf.value.decode("utf-8", "replace")


def extract_page_chars(textpage: pdfium.PdfTextPage, page_no: int, with_fonts: bool = False) -> list[CharInfo]:
    """Collect text, box, font size, colours and render mode for every char on a page.
    ``with_fonts`` also resolves each char's font name (cached per text object) and
    whether pdfium could map it to Unicode."""
    tp = textpage.raw
    n = pdfium_c.FPDFText_CountChars(tp)
    chars: list[CharInfo] = []
    rect = pdfium_c.FS_RECTF()
    matrix = pdfium_c.FS_MATRIX()
    font_cache: dict[int, str] = {}
    pending_high: int | None = None
    for i in range(n):
        cp = pdfium_c.FPDFText_GetUnicode(tp, i)
        # pdfium may hand back UTF-16 surrogate halves on some platforms.
        if 0xD800 <= cp <= 0xDBFF:
            pending_high = cp
            continue
        if 0xDC00 <= cp <= 0xDFFF and pending_high is not None:
            cp = 0x10000 + ((pending_high - 0xD800) << 10) + (cp - 0xDC00)
        pending_high = None
        try:
            text = chr(cp)
        except ValueError:
            text = "�"

        if pdfium_c.FPDFText_GetLooseCharBox(tp, i, ctypes.byref(rect)):
            bbox = (rect.left, rect.bottom, rect.right, rect.top)
        else:
            bbox = (0.0, 0.0, 0.0, 0.0)

        obj = pdfium_c.FPDFText_GetTextObject(tp, i)
        mode = pdfium_c.FPDFTextObj_GetTextRenderMode(obj) if obj else -1
        font = ""
        unmapped = False
        rotated = False
        if with_fonts:
            key = ctypes.cast(obj, ctypes.c_void_p).value if obj else 0
            if key not in font_cache:
                font_cache[key] = _font_name(tp, i)
            font = font_cache[key]
            unmapped = pdfium_c.FPDFText_HasUnicodeMapError(tp, i) == 1 or text == "\ufffd"
            angle = abs(pdfium_c.FPDFText_GetCharAngle(tp, i)) % 3.141592653589793
            rotated = 0.2 < angle < 3.141592653589793 - 0.2

        chars.append(
            CharInfo(
                index=i,
                text=text,
                page=page_no,
                bbox=bbox,
                font_size=_effective_font_size(tp, i, matrix),
                fill=_color(pdfium_c.FPDFText_GetFillColor, tp, i),
                stroke=_color(pdfium_c.FPDFText_GetStrokeColor, tp, i),
                render_mode=int(mode),
                is_generated=pdfium_c.FPDFText_IsGenerated(tp, i) == 1,
                font_name=font,
                unmapped=unmapped,
                rotated=rotated,
            )
        )
    return chars


def extract_chars(pdf: pdfium.PdfDocument, with_fonts: bool = False) -> list[CharInfo]:
    """Return a CharInfo for every character on every page of an open document."""
    out: list[CharInfo] = []
    for page_index in range(len(pdf)):
        page = pdf[page_index]
        textpage = page.get_textpage()
        try:
            out.extend(extract_page_chars(textpage, page_index + 1, with_fonts))
        finally:
            textpage.close()
            page.close()
    return out


class PlumberLayer:
    """Lazily opened pdfplumber (pdfminer) view of the same file.

    pdfium's text page drops repeated identical characters drawn at the same
    position (its fake-bold de-duplication). Zero-advance characters all sit
    at one position, so a 16-char zero-width run collapses to one char per
    code point. pdfminer keeps every glyph, so pages that show any hidden
    Unicode in pdfium's layer are re-read here for the Unicode scan.
    """

    def __init__(self, path) -> None:
        self._path = path
        self._pdf = None
        self._failed = False

    def page_text(self, page_index: int) -> tuple[str, list[BBox]] | None:
        """Text and per-char boxes (PDF coords) for a page, or None on failure."""
        import pdfplumber  # local: only paid for pages that need it

        if self._failed:
            return None
        try:
            if self._pdf is None:
                self._pdf = pdfplumber.open(self._path)
            page = self._pdf.pages[page_index]
            h = float(page.height)
            parts: list[str] = []
            boxes: list[BBox] = []
            for ch in page.chars:
                # a glyph may map to several code points (ligatures, "(cid:N)"
                # for unmapped glyphs); repeat its box so offsets stay aligned
                box = (float(ch["x0"]), h - float(ch["bottom"]), float(ch["x1"]), h - float(ch["top"]))
                parts.append(ch["text"])
                boxes.extend([box] * len(ch["text"]))
            return "".join(parts), boxes
        except Exception as exc:  # noqa: BLE001 - third-party parser on untrusted input
            log.warning("pdfplumber layer unavailable for page %d: %s", page_index + 1, exc)
            self._failed = True
            return None

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None


def count_page_objects(page: pdfium.PdfPage) -> tuple[int, int]:
    """(images, paths) painted on a page, including inside form XObjects."""
    counts = {"image": 0, "path": 0}

    def walk(count: int, get) -> None:
        for i in range(count):
            obj = get(i)
            kind = pdfium_c.FPDFPageObj_GetType(obj)
            if kind == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                counts["image"] += 1
            elif kind == pdfium_c.FPDF_PAGEOBJ_PATH:
                counts["path"] += 1
            elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                walk(pdfium_c.FPDFFormObj_CountObjects(obj),
                     lambda j, form=obj: pdfium_c.FPDFFormObj_GetObject(form, j))

    walk(pdfium_c.FPDFPage_CountObjects(page.raw), lambda i: pdfium_c.FPDFPage_GetObject(page.raw, i))
    return counts["image"], counts["path"]
