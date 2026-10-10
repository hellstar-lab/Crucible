"""Structure extraction for any PDF report or resume: deterministic, no model.

Everything is built from VISIBLE characters only (Shield's hidden text never
becomes a heading, a claim or a skill) in display space (rotation-aware).

  headings        font-size histogram -> body_size (the mode). A line is a heading when its
                  median size > body_size * 1.15, or it is (almost) all bold, under 80 chars
                  and a single column segment. Levels rank the distinct heading sizes. When
                  neither signal exists (monospaced / single-font text), numbering
                  patterns (^\\d+(\\.\\d+)*\\s+\\S) take over.
  sections        the text between headings: the unit Phase 4 consumes instead of a flat blob.
  tables          "lines" strategy first. The "text" strategy runs ONLY inside a bounded region
                  already known to be tabular (after a "Table N" caption, or a block of >= 3 lines
                  with >= 2 aligned numeric columns). Every candidate is validated; failures are
                  discarded. Never run on a whole page: that invents a grid out of paragraphs.
  links           annotation URIs plus bare URLs in the text, de-duplicated, host-classified,
                  GitHub owner/repo extracted so Phase 3.6 can consume them.
  numeric_claims  number + unit with 60 characters of verbatim context. Interpreted by nobody.
  references      the References/Bibliography section plus every DOI and arXiv id.

pdfplumber is used ONLY for table geometry and ONLY on pages where its coordinates are
provably the same as ours (rotation 0, effective box == MediaBox with origin (0,0)). Its
coordinates are wrong on offset or rotated pages (see geometry.py), so those pages report
"tables not measured" instead of risking wrong cells.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

from backend.contracts import (Heading, LinkRef, NumericClaim, ReferencesBlock, Section, StructureBlock, Table)

from . import config, hygiene, linkcheck
from .lines import Line, build_lines, display_box

log = logging.getLogger(__name__)

_NUMBERING = re.compile(r"^\d+(?:\.\d+)*[.)]?\s+\S")
_CAPTION = re.compile(r"(?i)^(table|tab\.)\s*\d+")
_BULLET = re.compile(r"^\s*(?:[-•*·▪●◦]\s|\d+[.)]\s|\([a-z0-9]\)\s|\[\d+\]\s)")
_REFERENCES = re.compile(r"(?i)^(?:\d+(?:\.\d+)*\.?\s+)?(references|bibliography|works cited|citations)\s*$")
_NUM_TOKEN = re.compile(r"^[\(\[]?[-+$€£]?\d[\d,]*(?:\.\d+)?%?[\)\]]?$")
_NUMERIC_CLAIM = re.compile(
    r"(?<![\w.,/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<sp>\s?)"
    r"(?P<unit>%|req/s|requests?/s|hours?|hrs?|users|LOC|ms|GB|MB|KB|TB|s|x|×)(?![\w/])")
_NO_SPACE_UNITS = {"s", "x", "×"}
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")
_ARXIV = re.compile(r"(?i)arxiv:\s?(\d{4}\.\d{4,5})(?:v\d+)?")
CONTEXT_CHARS = 60
GUTTER_MIN_RIGHT_FRACTION = 0.2  # median width of right-of-gutter text, as a fraction of page width
MAX_CLAIMS = 200
MAX_REFERENCES = 300


@dataclass
class Unit:
    """One heading or paragraph, in reading order."""
    kind: str  # "heading" | "paragraph"
    text: str
    page: int
    top: float
    size: float
    lines: list = field(default_factory=list, repr=False)
    heading: Heading | None = None
    section_index: int = -1


@dataclass
class Extracted:
    block: StructureBlock
    full_text: str
    units: list[Unit]
    section_of_line: dict[int, int]  # id(Line) -> index into block.sections
    mojibake_fixed: bool = False
    text_pages: list[int] = field(default_factory=list)
    lines_by_page: dict[int, list] = field(default_factory=dict, repr=False)  # reading-order lines (PDF only)
    body_by_page: dict[int, float | None] = field(default_factory=dict)


# --------------------------------------------------------------------------
# reading order
# --------------------------------------------------------------------------


def _norm_running(text: str) -> str:
    return " ".join(re.sub(r"\d+", "#", text.lower()).split())


def mark_running(lines_by_page: dict[int, list[Line]], heights: dict[int, float]) -> set[tuple[int, int]]:
    """(page, id(line)) of lines that repeat in the top/bottom band of enough pages."""
    n_pages = len(lines_by_page)
    if n_pages < config.RUNNING_LINE_MIN_PAGES:
        return set()
    seen: dict[str, set[int]] = {}
    for page, lines in lines_by_page.items():
        band = config.RUNNING_BAND_FRACTION * heights[page]
        for ln in lines:
            if ln.top < band or ln.bottom > heights[page] - band:
                seen.setdefault(_norm_running(ln.text), set()).add(page)
    need = max(config.RUNNING_LINE_MIN_PAGES, config.RUNNING_LINE_MIN_FRACTION * n_pages)
    repeating = {k for k, pgs in seen.items() if len(pgs) >= need and k}
    out = set()
    for page, lines in lines_by_page.items():
        band = config.RUNNING_BAND_FRACTION * heights[page]
        for ln in lines:
            if (ln.top < band or ln.bottom > heights[page] - band) and _norm_running(ln.text) in repeating:
                out.add((page, id(ln)))
    return out


def _gutter(lines: list[Line], width: float) -> float | None:
    """x of a vertical gutter splitting a page into two text columns, or None."""
    body = [ln for ln in lines if ln.n_chars >= 12]
    if len(body) < 8:
        return None
    cover = [0] * (int(width) + 2)
    for ln in body:
        runs = sorted(ln.runs, key=lambda r: r.x0)
        seg_start, prev = runs[0].x0, runs[0]
        for r in runs[1:] + [None]:
            if r is None or r.x0 - prev.x1 > max(1.5 * ln.size, 10.0):
                for x in range(int(seg_start), min(int(prev.x1) + 1, len(cover) - 1)):
                    cover[x] += 1
                if r is not None:
                    seg_start = r.x0
            if r is not None:
                prev = r
    n = len(body)
    lo, hi = int(0.3 * width), int(0.7 * width)
    best, run_start = None, None
    for x in range(lo, hi + 1):
        if cover[x] <= 0.15 * n:
            run_start = x if run_start is None else run_start
            if x - run_start >= 11:
                best = (run_start + x) / 2 if best is None or abs((run_start + x) / 2 - width / 2) < abs(best - width / 2) else best
        else:
            run_start = None
    if best is None:
        return None
    # a two-column page: most lines have a run wholly left of the gutter AND a run wholly right of it
    left = sum(1 for ln in body if any(r.x1 <= best + 2 for r in ln.runs))
    right = sum(1 for ln in body if any(r.x0 >= best - 2 for r in ln.runs))
    both = sum(1 for ln in body if any(r.x1 <= best + 2 for r in ln.runs) and any(r.x0 >= best - 2 for r in ln.runs))
    if not (left >= 0.4 * n and right >= 0.4 * n and both >= 0.25 * n):
        return None
    # right-aligned dates beside job titles also leave a gutter; a real right COLUMN holds text blocks, not 90pt dates
    widths = sorted(max(r.x1 for r in rr) - min(r.x0 for r in rr) for ln in body if (rr := [r for r in ln.runs if r.x0 >= best - 2]))
    return best if widths and widths[len(widths) // 2] >= GUTTER_MIN_RIGHT_FRACTION * width else None


def reading_order(pd, lines: list[Line]) -> list[Line]:
    """Top-to-bottom for single-column pages. For two columns, full-width lines act as separators and,
    between them, the left column is read before the right column."""
    g = _gutter(lines, pd.width)
    if g is None:
        return sorted(lines, key=lambda ln: (ln.top, ln.x0))
    spanning = sorted((ln for ln in lines if any(r.x0 < g - 2 and r.x1 > g + 2 for r in ln.runs)), key=lambda ln: ln.top)
    left: list[Line] = []
    right: list[Line] = []
    for ln in lines:
        if ln in spanning:
            continue
        for bucket, want_left in ((left, True), (right, False)):
            chars = [c for c in ln.chars if ((display_box(c.bbox, pd)[0] + display_box(c.bbox, pd)[2]) / 2 < g) == want_left]
            if chars:
                bucket += build_lines(pd, chars)
    edges = [float("-inf")] + [ln.top for ln in spanning] + [float("inf")]
    out: list[Line] = []
    for i in range(len(edges) - 1):
        if i > 0:
            out.append(spanning[i - 1])
        for bucket in (left, right):
            out += sorted((ln for ln in bucket if edges[i] <= ln.top < edges[i + 1]), key=lambda ln: ln.top)
    return out


# --------------------------------------------------------------------------
# headings
# --------------------------------------------------------------------------


def _alpha(text: str) -> bool:
    return sum(c.isalpha() for c in text) >= max(2, len(text) * 0.4)


def find_headings(lines: list[Line], body: float) -> tuple[list[Line], dict[int, tuple[str, float]]]:
    """(lines, {id(line): (method, size)}). A heading that wraps onto a second line of the same size is
    merged into ONE Line, so the returned list replaces the input list."""
    out: list[Line] = []
    info: dict[int, tuple[str, float]] = {}
    for ln in lines:
        method = None
        if ln.n_chars and _alpha(ln.text) and len(ln.text) <= 200:
            if ln.size > body * config.HEADING_SIZE_RATIO and ln.size >= 8:
                method = "size"
            elif (ln.bold_fraction >= config.HEADING_BOLD_MIN_FRACTION and len(ln.text) < config.HEADING_BOLD_MAX_CHARS
                  and len(ln.segments) == 1 and ln.size >= body * 0.95 and not ln.text.rstrip().endswith((",", ";", ":"))):
                method = "bold"
        if method is None:
            out.append(ln)
            continue
        size = round(ln.size * 2) / 2
        prev = out[-1] if out else None
        if (prev is not None and id(prev) in info and method == "size" and info[id(prev)] == ("size", size)
                and prev.page == ln.page and 0 <= ln.top - prev.bottom < 0.9 * ln.size and not prev.text.rstrip().endswith(".")):
            merged = Line(prev.page, prev.text + " " + ln.text, prev.x0, max(prev.x1, ln.x1), prev.top, ln.bottom, prev.size,
                          prev.bold_fraction, prev.segments + ln.segments, prev.runs + ln.runs, prev.n_chars + ln.n_chars,
                          prev.chars + ln.chars)
            del info[id(prev)]
            out[-1] = merged
            info[id(merged)] = ("size", size)
        else:
            out.append(ln)
            info[id(ln)] = (method, size)
    return out, info


def numbering_headings(lines: list[Line]) -> list[tuple[Line, int]]:
    out = []
    for ln in lines:
        t = ln.text.strip()
        if _NUMBERING.match(t) and len(t) < 100 and not t.endswith((".", ",", ";", ":")) and len(ln.segments) == 1:
            first = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", t)[:1]
            if first.isupper() or first.isdigit():
                depth = re.match(r"^(\d+(?:\.\d+)*)", t).group(1).count(".") + 1
                out.append((ln, depth))
    return out


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


def validate_table(rows: list[list[str]]) -> bool:
    """>= 2 rows, >= 2 columns, the same column count in every row, >= 60% non-empty cells."""
    if len(rows) < config.TABLE_MIN_ROWS:
        return False
    widths = {len(r) for r in rows}
    if len(widths) != 1 or next(iter(widths)) < config.TABLE_MIN_COLS:
        return False
    cells = [c for r in rows for c in r]
    return sum(1 for c in cells if c.strip()) / len(cells) >= config.TABLE_MIN_NONEMPTY


def _safe_for_plumber(pd) -> bool:
    """pdfplumber's coordinates equal display space only when there is no rotation and the page is the
    MediaBox starting at (0,0)."""
    m, e = pd.boxes.media, pd.boxes.effective
    return pd.rotation % 360 == 0 and m[0] == 0 and m[1] == 0 and e == m


def _cell_text(pd, bbox, hygiene_clean) -> tuple[str, bool]:
    x0, top, x1, bottom = bbox
    chars = []
    for c in pd.chars:
        if c.index in pd.hidden or c.is_generated or not c.text:
            continue
        d = display_box(c.bbox, pd)
        cx, cy = (d[0] + d[2]) / 2, (d[1] + d[3]) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom:
            chars.append(c)
    from .lines import text_from_chars

    return hygiene_clean(text_from_chars(chars, pd))


def numeric_block_regions(lines: list[Line]) -> list[tuple[float, float, float, float]]:
    """Bounded regions made of >= 3 consecutive lines with >= 2 numeric segments whose x positions align."""
    def numeric_cols(ln: Line):
        runs = sorted(ln.runs, key=lambda r: r.x0)
        segs, cur = [], [runs[0]]
        for r in runs[1:]:
            if r.x0 - cur[-1].x1 > max(1.5 * ln.size, 10.0):
                segs.append(cur)
                cur = [r]
            else:
                cur.append(r)
        segs.append(cur)
        out = []
        for seg in segs:
            text = " ".join(r.text.strip() for r in seg)
            if _NUM_TOKEN.match(text.replace(" ", "")):
                out.append(((seg[0].x0 + seg[-1].x1) / 2, seg[0].x1))
        return out, len(segs)

    regions, block = [], []
    infos = [numeric_cols(ln) for ln in lines]
    for i, ln in enumerate(lines):
        cols, nseg = infos[i]
        ok = len(cols) >= 2
        if ok and block:
            prev_cols = infos[block[-1]][0]
            aligned = len(cols) == len(prev_cols) and all(abs(a[1] - b[1]) <= 6 or abs(a[0] - b[0]) <= 6 for a, b in zip(cols, prev_cols))
            ok = aligned and ln.top - lines[block[-1]].bottom < 1.2 * ln.size
        if ok:
            block.append(i)
        else:
            if len(block) >= 3:
                regions.append(block)
            block = [i] if len(cols) >= 2 else []
    if len(block) >= 3:
        regions.append(block)
    out = []
    for blk in regions:
        first = blk[0]
        if first > 0 and infos[first - 1][1] == infos[first][1] and not _CAPTION.match(lines[first - 1].text):
            blk = [first - 1] + blk  # the header row above has the same column count
        sel = [lines[i] for i in blk]
        out.append((min(l.x0 for l in sel) - 3, min(l.top for l in sel) - 2, max(l.x1 for l in sel) + 3, max(l.bottom for l in sel) + 2))
    return out


def caption_regions(lines: list[Line], width: float, height: float, body: float | None) -> list[tuple[Line, tuple[float, float, float, float]]]:
    """Region below each 'Table N' caption, ending at the next caption or large heading, a big vertical gap,
    a jump to another column, or the page end."""
    out = []
    gaps = [b.top - a.bottom for a, b in zip(lines, lines[1:]) if 0 < b.top - a.bottom < 40]
    typical = statistics.median(gaps) if gaps else 3.0
    for i, ln in enumerate(lines):
        if not _CAPTION.match(ln.text.strip()):
            continue
        sel: list[Line] = []
        prev = ln
        for nxt in lines[i + 1:]:
            if len(sel) >= 2 and nxt.top - prev.bottom > max(2.5 * typical, 14.0):
                break
            heading_like = body is not None and nxt.size > body * config.HEADING_SIZE_RATIO and nxt.bold_fraction > 0.5
            if _CAPTION.match(nxt.text.strip()) or heading_like or nxt.top < prev.top:
                break
            sel.append(nxt)
            prev = nxt
        if len(sel) >= 2:
            x0 = min(ln.x0, min(l.x0 for l in sel)) - 5
            x1 = max(l.x1 for l in sel) + 5
            out.append((ln, (max(x0, 0.0), ln.bottom + 1, min(x1, width), sel[-1].bottom + 2)))
    return out


def find_tables(path: str, pages: list, lines_by_page: dict[int, list[Line]], deadline: float, notes: list[str],
                body: float | None = None) -> list[Table] | None:  # lines_by_page: reading-order lines per page
    """Validated tables, or None when no page could be measured (never an empty list for 'not measured')."""
    import pdfplumber

    measured, skipped, tables = 0, [], []
    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"tables not measured: pdfplumber could not open the file ({type(exc).__name__})")
        return None
    with pdf:
        for pd in pages:
            if time.monotonic() > deadline:
                notes.append(f"table detection stopped at page {pd.page}: time budget")
                break
            if not _safe_for_plumber(pd):
                skipped.append(pd.page)
                continue
            lines = sorted(lines_by_page.get(pd.page, []), key=lambda ln: (ln.top, ln.x0))
            captions = caption_regions(lines, pd.width, pd.height, body)
            blocks = numeric_block_regions(lines) if lines else []
            need_lines = pd.paths >= 4
            if not (need_lines or captions or blocks):
                measured += 1
                continue
            try:
                pl = pdf.pages[pd.page - 1]
                found: list[tuple[str, tuple, list]] = []
                if need_lines:
                    for t in pl.find_tables():
                        found.append(("lines", t.bbox, [[c for c in row.cells] for row in t.rows]))
                regions = [r for _l, r in captions] + blocks
                for reg in regions:
                    x0, top, x1, bottom = (max(0.0, reg[0]), max(0.0, reg[1]), min(pd.width, reg[2]), min(pd.height, reg[3]))
                    if x1 - x0 < 20 or bottom - top < 10:
                        continue
                    sub = pl.crop((x0, top, x1, bottom))
                    for t in sub.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3,
                                              "join_tolerance": 3, "intersection_tolerance": 5}):
                        found.append(("text_region", t.bbox, [[c for c in row.cells] for row in t.rows]))
            except Exception as exc:  # noqa: BLE001 - a page pdfplumber cannot parse is unmeasured, not empty
                skipped.append(pd.page)
                log.warning("pdfplumber failed on page %d: %s", pd.page, exc)
                continue
            measured += 1
            line_boxes = [bbox for strategy, bbox, _rows in found if strategy == "lines"]
            seen: list[Table] = []
            for strategy, bbox, cell_rows in found:
                if strategy == "text_region" and any(_overlap(bbox, lb) > 0.5 for lb in line_boxes):
                    continue  # the lines strategy already found this table
                rows = [[(_cell_text(pd, c, hygiene.clean)[0] if c else "") for c in row] for row in cell_rows]
                rows = [r for r in rows if any(c.strip() for c in r)]  # all-empty rows are text-strategy artefacts, not rows
                if strategy == "text_region":
                    rows = _segment_cells(rows, bbox, lines_by_page.get(pd.page, []), hygiene.clean) or rows
                if not validate_table(rows) or any(t.rows == rows for t in seen):
                    continue
                cap = next((cl.text.strip() for cl, _r in captions if cl.top < bbox[1] and bbox[1] - cl.bottom < 40), None)
                seen.append(Table(page=pd.page, caption=cap, strategy=strategy, n_rows=len(rows), n_cols=len(rows[0]), rows=rows))
            tables.extend(seen)
    if skipped:
        notes.append(f"tables not measured on pages {skipped[:20]}: rotated or offset pages (pdfplumber coordinates unsafe)")
    return tables if measured else None


def _segment_cells(rows: list[list[str]], bbox, lines: list[Line], clean) -> list[list[str]] | None:
    """pdfplumber's text strategy derives column edges from word left edges, which splits right-aligned
    columns wrongly ("p95 (ms)" -> "p95" | "(ms)"). When our own gap-based segments give the same
    column count on every row, and that count matches pdfplumber's, use them for the cell text."""
    x0, top, x1, bottom = bbox
    inside = [ln for ln in lines if top - 2 <= (ln.top + ln.bottom) / 2 <= bottom + 2 and ln.x0 >= x0 - 6 and ln.x1 <= x1 + 6]
    if not inside or len(inside) != len(rows):
        return None
    counts = {len(ln.segments) for ln in inside}
    if len(counts) != 1 or counts != {len(rows[0])}:
        return None
    return [[clean(seg)[0] for seg in ln.segments] for ln in sorted(inside, key=lambda ln: ln.top)]


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return inter / area


# --------------------------------------------------------------------------
# claims, references, links
# --------------------------------------------------------------------------


def numeric_claims(units: list[Unit], sections: list[Section]) -> list[NumericClaim]:
    out: list[NumericClaim] = []
    for u in units:
        if u.kind != "paragraph":
            continue
        sec = sections[u.section_index] if 0 <= u.section_index < len(sections) else None
        if sec and sec.heading and _REFERENCES.match(sec.heading.strip()):
            continue
        for m in _NUMERIC_CLAIM.finditer(u.text):
            unit, value = m.group("unit"), m.group(1)
            if unit in _NO_SPACE_UNITS and m.group("sp"):
                continue
            if unit == "s" and re.fullmatch(r"(19|20)\d\d", value):  # "the 1990s"
                continue
            out.append(NumericClaim(value=value, unit=unit, match=m.group(0), page=u.page,
                                    context_before=u.text[max(0, m.start() - CONTEXT_CHARS):m.start()],
                                    context_after=u.text[m.end():m.end() + CONTEXT_CHARS],
                                    section=sec.heading if sec else None))
            if len(out) >= MAX_CLAIMS:
                return out
    return out


def references_block(units: list[Unit], sections: list[Section], full_text: str) -> ReferencesBlock:
    entries: list[str] = []
    found = False
    for i, sec in enumerate(sections):
        if sec.heading and _REFERENCES.match(sec.heading.strip()):
            found = True
            body = [u.text for u in units if u.section_index == i and u.kind == "paragraph"]
            joined = "\n".join(body)
            if re.search(r"(?m)^\s*\[\d+\]", joined):
                parts = re.split(r"(?m)^\s*(?=\[\d+\])", joined)
            elif re.search(r"(?m)^\s*\d+\.\s", joined):
                parts = re.split(r"(?m)^\s*(?=\d+\.\s)", joined)
            else:
                parts = body
            entries += [" ".join(p.split()) for p in parts if p.strip()]
    dois = sorted({m.group(0).rstrip(".,;)") for m in _DOI.finditer(full_text)})
    arxiv = sorted({m.group(1) for m in _ARXIV.finditer(full_text)})
    return ReferencesBlock(section_found=found, entries=entries[:MAX_REFERENCES], dois=dois, arxiv_ids=arxiv)


def _norm_url(url: str) -> str:
    u = url.strip().lower().rstrip("/")
    u = re.sub(r"^https?://", "", u)
    return re.sub(r"^www\.", "", u)


def build_links(link_obs: list, full_text: str) -> list[LinkRef]:
    refs: dict[str, LinkRef] = {}
    for o in link_obs:
        if o.action != "URI" or not o.target:
            continue
        url = o.target.strip()
        host = linkcheck.uri_host(url)
        key = _norm_url(url)
        acct, tgt = linkcheck.github_target(url)
        ref = refs.get(key)
        if ref is None:
            refs[key] = LinkRef(url=url, host=host, kind="email" if url.lower().startswith("mailto:") else linkcheck.classify_host(host),
                                sources=["annotation"], page=o.page, display_text=o.display_text or None, mismatch=bool(o.mismatch),
                                github_account=acct, github_target=tgt)
        else:
            ref.mismatch = ref.mismatch or bool(o.mismatch)
    for url in linkcheck.find_bare_urls(full_text):
        key = _norm_url(url)
        if key in refs:
            if "text" not in refs[key].sources:
                refs[key].sources.append("text")
            continue
        host = linkcheck.uri_host(url)
        acct, tgt = linkcheck.github_target(url)
        refs[key] = LinkRef(url=url, host=host, kind=linkcheck.classify_host(host), sources=["text"], github_account=acct, github_target=tgt)
    return list(refs.values())


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def _paragraphs(lines: list[Line], heading_ids: set[int]) -> list[tuple[list[Line], bool]]:
    """Group reading-order lines into (lines, is_heading) units."""
    units: list[tuple[list[Line], bool]] = []
    cur: list[Line] = []
    for ln in lines:
        if id(ln) in heading_ids:
            if cur:
                units.append((cur, False))
                cur = []
            units.append(([ln], True))
            continue
        if cur:
            prev = cur[-1]
            gap = ln.top - prev.bottom
            new = (ln.page != prev.page or ln.top < prev.top - 1 or gap > 0.55 * max(ln.size, prev.size)
                   or _BULLET.match(ln.text) is not None or abs(ln.size - prev.size) > 1.5)
            if new:
                units.append((cur, False))
                cur = []
        cur.append(ln)
    if cur:
        units.append((cur, False))
    return units


def build_sections(units: list[Unit]) -> tuple[list[Section], str]:
    """Group units under their headings (setting ``unit.section_index``) and join the cleaned full text."""
    sections: list[Section] = []
    parts: list[list[str]] = []
    pos, cur = 0, -1
    full_parts: list[str] = []
    for u in units:
        if u.kind == "heading" or cur == -1:
            sections.append(Section(heading=u.text if u.kind == "heading" else None, level=u.heading.level if u.heading else None,
                                    page_start=u.page, page_end=u.page, char_start=pos, char_end=pos, text=""))
            parts.append([])
            cur = len(sections) - 1
        u.section_index = cur
        full_parts.append(u.text)
        if u.kind == "paragraph":
            parts[cur].append(u.text)
        sections[cur].page_end = max(sections[cur].page_end, u.page)
        pos += len(u.text) + 1
        sections[cur].char_end = pos - 1
    for sec, ps in zip(sections, parts):
        sec.text = "\n".join(ps)
    return sections, "\n".join(full_parts)


def extract(path: str, pages: list, link_obs: list, text_page_numbers: set[int], deadline: float) -> Extracted:
    notes: list[str] = []
    raw_by_page: dict[int, list[Line]] = {}
    heights: dict[int, float] = {}
    page_by_no = {pd.page: pd for pd in pages}
    for pd in pages:
        if pd.page in text_page_numbers:
            raw_by_page[pd.page] = build_lines(pd)
            heights[pd.page] = pd.height
    running = mark_running(raw_by_page, heights)
    ordered_by_page: dict[int, list[Line]] = {}
    for pno, lines in sorted(raw_by_page.items()):
        ordered_by_page[pno] = reading_order(page_by_no[pno], [ln for ln in lines if (pno, id(ln)) not in running])
    ordered = [ln for pno in sorted(ordered_by_page) for ln in ordered_by_page[pno]]

    sizes: Counter = Counter()
    for ln in ordered:
        sizes[round(ln.size * 2) / 2] += ln.n_chars
    body = sizes.most_common(1)[0][0] if sizes else None
    info: dict[int, tuple[str, float]] = {}
    depth_of: dict[int, int] = {}
    if body:
        ordered, info = find_headings(ordered, body)
        if not info:  # monospaced / single-font text: size and weight say nothing, numbering must
            for ln, depth in numbering_headings(ordered):
                info[id(ln)] = ("numbering", float(depth))
                depth_of[id(ln)] = depth
    level_of = {s: i for i, s in enumerate(sorted({s for m, s in info.values() if m in ("size", "bold")}, reverse=True), start=1)}

    units: list[Unit] = []
    mojibake = False
    for group, is_heading in _paragraphs(ordered, set(info)):
        text, fixed = hygiene.clean(" ".join(l.text for l in group))
        mojibake = mojibake or fixed
        first = group[0]
        if is_heading:
            method, size = info[id(first)]
            level = depth_of.get(id(first)) or level_of.get(size, len(level_of) + 1)
            h = Heading(text=text, level=level, page=first.page, size=None if method == "numbering" else size, method=method)  # type: ignore[arg-type]
            units.append(Unit("heading", text, first.page, first.top, first.size, group, h))
        else:
            units.append(Unit("paragraph", text, first.page, first.top, first.size, group))

    sections, full_text = build_sections(units)

    tables = (find_tables(path, [pd for pd in pages if pd.page in text_page_numbers], ordered_by_page, deadline, notes, body)
              if text_page_numbers else None)
    block = StructureBlock(
        headings=[u.heading for u in units if u.heading], sections=sections, tables=tables, links=build_links(link_obs, full_text),
        numeric_claims=numeric_claims(units, sections), references=references_block(units, sections, full_text),
        body_size=body, notes=notes)
    section_of_line = {id(ln): u.section_index for u in units for ln in u.lines}
    body_by_page: dict[int, float | None] = {}
    for pno, lines in ordered_by_page.items():
        per: Counter = Counter()
        for ln in lines:
            per[round(ln.size * 2) / 2] += ln.n_chars
        body_by_page[pno] = per.most_common(1)[0][0] if per else None
    return Extracted(block, full_text, units, section_of_line, mojibake, sorted(raw_by_page), ordered_by_page, body_by_page)
