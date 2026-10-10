"""Characters -> lines, in reading order.

Works on pdfium's per-character data, in DISPLAY space (x right, y down, origin
at the top-left of the visible page), so /Rotate and offset boxes need no special
cases downstream. Only visible characters are used: text Shield judged hidden
never becomes a heading, a claim or a skill.

A space is inserted wherever the gap between neighbouring glyphs is wide enough
(LaTeX and many generators draw word gaps as positioning, not as space glyphs).
A much wider gap splits a line into SEGMENTS (table columns, a job title with a
right-aligned date); headings never span segments and font-change checks never
cross them.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .extract import CharInfo

WORD_GAP_EM = 0.12  # a gap wider than this many em is a word space (was 0.18; measured on real PDFs: within-word gaps <= 0.10 em, word gaps >= 0.14 em)
SEGMENT_GAP_EM = 1.5  # ...and wider than this (and >= SEGMENT_GAP_MIN_PT) separates segments
SEGMENT_GAP_MIN_PT = 10.0
LINE_TOLERANCE_EM = 0.55

_SUBSET = re.compile(r"^[A-Z]{6}\+")
_BOLD = re.compile(r"(?i)(bold|black|heavy|semibold|demibold|extrabold|-bd\b|bd$|cmbx|cmssbx|-medi\b|medi$|-demi)")
_ITALIC = re.compile(r"(?i)(italic|oblique|-it\b|cmti|cmsl|-ital)")
_STYLE_TOKENS = re.compile(
    r"(?i)(bolditalic|boldoblique|bold|italic|oblique|regular|regu|roman|medium|medi|light|semibold|demibold|black|heavy|thin|"
    r"condensed|narrow|extrabold|extralight|book|demi|ital|obli|psmt|ps|mt|it|bd|bi)$")
SYMBOL_FAMILIES = ("symbol", "zapf", "dingbat", "wingding", "fontawesome", "simpleicons", "cmsy", "cmmi", "cmex", "msam",
                   "msbm", "stix", "mathjax", "mathematica", "lasy", "wasy", "cmbsy", "cmmib")
MONO_FAMILIES = ("courier", "mono", "consolas", "menlo", "monaco", "cmtt", "sftt", "lucidaconsole", "inconsolata", "typewriter")


def strip_subset(name: str) -> str:
    return _SUBSET.sub("", name or "")


def is_bold(name: str) -> bool:
    return bool(_BOLD.search(strip_subset(name)))


def is_italic(name: str) -> bool:
    return bool(_ITALIC.search(strip_subset(name)))


def font_family(name: str) -> str:
    """Face family with subset prefix and style words removed: 'AAAAAA+Arial-BoldMT' -> 'arial'."""
    base = strip_subset(name).lower()
    if not base:
        return ""
    cm = re.match(r"cm(sy|mi|ex|tt|r|bx|ti|sl|ss|ssbx|csc|ff|u|b|bsy|mib)\d+", base)  # Computer Modern: cmr10, cmbx12 ...
    if cm:
        return "cm" + cm.group(1) if cm.group(1) in ("sy", "mi", "ex", "tt", "bsy", "mib") else "computer-modern"
    base = re.split(r"[-,_ ]", base)[0]
    prev = None
    while prev != base:
        prev, base = base, _STYLE_TOKENS.sub("", base)
    base = base or strip_subset(name).lower()
    return "times" if base.startswith("times") else base


def is_symbol_family(family: str) -> bool:
    return any(family.startswith(s) or s in family for s in SYMBOL_FAMILIES)


def is_mono_family(family: str) -> bool:
    return any(s in family for s in MONO_FAMILIES)


def display_box(bbox, pd) -> tuple[float, float, float, float]:
    """User-space box -> display space (x right, y DOWN) for page ``pd`` (a PageData)."""
    L, B, R, T = pd.boxes.effective
    l, b, r, t = bbox
    rot = pd.rotation % 360
    if rot == 90:
        return (b - B, l - L, t - B, r - L)
    if rot == 180:
        return (R - r, b - B, R - l, t - B)
    if rot == 270:
        return (T - t, R - r, T - b, R - l)
    return (l - L, T - t, r - L, T - b)


@dataclass
class Run:
    text: str
    family: str
    size: float
    bold: bool
    x0: float
    x1: float


@dataclass
class Line:
    page: int
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float  # median glyph size
    bold_fraction: float
    segments: list[str]
    runs: list[Run]
    n_chars: int
    chars: list[CharInfo] = field(default_factory=list, repr=False)

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _usable(c: CharInfo, hidden: frozenset[int]) -> bool:
    l, b, r, t = c.bbox
    if c.index in hidden or c.is_generated or c.rotated or r <= l or t <= b:
        return False
    ch = c.text
    return bool(ch) and (ch == " " or (ch.isprintable() and ord(ch[0]) not in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060)))


def text_from_chars(chars: list[CharInfo], pd) -> str:
    """Reading-order text of an arbitrary character selection (used for link rectangles)."""
    boxed = [(c, display_box(c.bbox, pd)) for c in chars if _usable(c, pd.hidden)]
    boxed.sort(key=lambda cb: (round((cb[1][1] + cb[1][3]) / 2 / 4.0), cb[1][0]))
    out, prev = [], None
    for c, d in boxed:
        if prev is not None:
            same_line = abs((d[1] + d[3]) / 2 - (prev[1][1] + prev[1][3]) / 2) < max(c.font_size, 1) * 0.8
            if not same_line:
                out.append(" ")
            elif d[0] - prev[1][2] > WORD_GAP_EM * max(c.font_size, 1):
                out.append(" ")
        out.append(c.text)
        prev = (c, d)
    return " ".join("".join(out).split())


def build_lines(pd, chars: list[CharInfo] | None = None) -> list[Line]:
    """Visible lines of one page (or of the given character subset), top to bottom.
    Reading order across columns is decided later (structure.py)."""
    items = []
    for c in (pd.chars if chars is None else chars):
        if _usable(c, pd.hidden):
            d = display_box(c.bbox, pd)
            items.append(((d[1] + d[3]) / 2, d, c))
    items.sort(key=lambda it: it[0])
    clusters: list[list] = []
    centre = size = 0.0
    for ymid, d, c in items:
        sz = max(c.font_size, 1.0)
        if clusters and abs(ymid - centre) <= LINE_TOLERANCE_EM * max(size, sz):
            clusters[-1].append((ymid, d, c))
            n = len(clusters[-1])
            centre += (ymid - centre) / n
            size = max(size, sz) if n < 3 else statistics.median(cc.font_size for _y, _d, cc in clusters[-1])
        else:
            clusters.append([(ymid, d, c)])
            centre, size = ymid, sz
    lines: list[Line] = []
    for cl in clusters:
        cl.sort(key=lambda it: it[1][0])
        sizes = [c.font_size for _y, _d, c in cl if c.text != " "]
        if not sizes:
            continue
        med = statistics.median(sizes)
        segs: list[list] = [[]]
        runs: list[Run] = []
        text_parts: list[str] = []
        prev = None
        new_segment = False
        for _y, d, c in cl:
            if prev is not None:
                gap = d[0] - prev[2]
                if gap > max(SEGMENT_GAP_EM * med, SEGMENT_GAP_MIN_PT):
                    segs.append([])
                    new_segment = True  # a run never spans a column gap
                elif gap > WORD_GAP_EM * med and c.text != " " and not (segs[-1] and segs[-1][-1].endswith(" ")):
                    segs[-1].append(" ")
            segs[-1].append(c.text)
            word_gap = prev is not None and d[0] - prev[2] > WORD_GAP_EM * med and c.text != " "
            prev = d
            if c.text != " ":
                fam, bold = font_family(c.font_name), is_bold(c.font_name)
                if (runs and not new_segment and runs[-1].family == fam and runs[-1].bold == bold
                        and abs(runs[-1].size - c.font_size) < 0.6):
                    r = runs[-1]
                    r.text += (" " if word_gap and not r.text.endswith(" ") else "") + c.text
                    r.x1 = d[2]
                else:
                    runs.append(Run(c.text, fam, c.font_size, bold, d[0], d[2]))
                new_segment = False
            elif runs and not runs[-1].text.endswith(" "):
                runs[-1].text += " "
        seg_texts = [" ".join("".join(s).split()) for s in segs if "".join(s).strip()]
        if not seg_texts:
            continue
        nonspace = [c for _y, _d, c in cl if c.text != " "]
        bold_fraction = sum(1 for c in nonspace if is_bold(c.font_name)) / len(nonspace) if nonspace else 0.0
        lines.append(Line(page=pd.page, text=" ".join(seg_texts), x0=min(d[0] for _y, d, _c in cl),
                          x1=max(d[2] for _y, d, _c in cl), top=min(d[1] for _y, d, _c in cl),
                          bottom=max(d[3] for _y, d, _c in cl), size=med, bold_fraction=bold_fraction,
                          segments=seg_texts, runs=runs, n_chars=len(nonspace), chars=[c for _y, _d, c in cl]))
    return lines
