"""DOCX analyzer: the same findings and structure as the PDF path, from the OOXML itself.

A DOCX has no pixels, so "hidden" is decided from run properties, resolved through the style chain
(run -> character style -> paragraph style -> basedOn ...):

  vanish        w:vanish on the run or a style it uses (Word's "Hidden" font effect)
  hidden_style  the vanish comes from a style rather than the run
  white_text    colour within BG_MATCH_DIST of the page background, with no shading/highlight behind the run
  tiny_font     effective size at or below DOCX_MIN_FONT_PT

Nothing is executed or fetched: macros (vbaProject.bin), embedded objects and external relationship
targets are reported from the zip listing and .rels files only. Zip size is checked BEFORE parsing.
Everything outside the ZIP bound is parsed by python-docx / lxml (entity resolution off).
"""

from __future__ import annotations

import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from backend.contracts import Heading, Provenance, SecurityFinding, HiddenFragment, Table, StructureBlock

from . import config, hygiene, linkcheck, structure
from .pdf_security import LinkObs, link_findings
from .unicode_scan import has_hidden_unicode, scan_text, strip_hidden_unicode

log = logging.getLogger(__name__)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_EXTERNAL = re.compile(rb'Target="([^"]+)"[^>]*TargetMode="External"|TargetMode="External"[^>]*Target="([^"]+)"')


@dataclass
class DocxResult:
    visible_text: str = ""
    fragments: list[HiddenFragment] = field(default_factory=list)
    findings: list[SecurityFinding] = field(default_factory=list)
    provenance: Provenance | None = None
    extracted: structure.Extracted | None = None
    errors: list[dict] = field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    mojibake_fixed: bool = False


def _on(el) -> bool:
    """A toggle property (<w:vanish/>, <w:vanish w:val="0"/>)."""
    if el is None:
        return False
    return el.get(W + "val", "true").lower() not in ("0", "false", "off")


def _lum_dist(rgb, bg) -> int:
    return sum(abs(a - b) for a, b in zip(rgb, bg))


def _style_chain(style):
    seen = 0
    while style is not None and seen < 20:
        yield style
        style = style.base_style
        seen += 1


class _Resolver:
    def __init__(self, doc):
        self.doc = doc
        bg = doc.element.find(f"{W}background")
        fill = bg.get(W + "color") if bg is not None else None
        self.background = tuple(int(fill[i:i + 2], 16) for i in (0, 2, 4)) if fill and re.fullmatch(r"[0-9A-Fa-f]{6}", fill) else (255, 255, 255)

    def run_props(self, run, paragraph):
        """(hidden_reason or None, colour tuple or None, size_pt or None, shaded bool)."""
        r = run._r
        rpr = r.find(f"{W}rPr")
        reason = None
        if rpr is not None and _on(rpr.find(f"{W}vanish")):
            reason = "vanish"
        styles = list(_style_chain(run.style)) if rpr is not None and rpr.find(f"{W}rStyle") is not None else []
        styles += list(_style_chain(paragraph.style))
        if reason is None:
            for st in styles:
                if st.font.hidden:
                    reason = "hidden_style"
                    break
        colour = None
        c = run.font.color
        if c is not None and c.rgb is not None:
            colour = tuple(c.rgb)
        else:
            for st in styles:
                if st.font.color is not None and st.font.color.rgb is not None:
                    colour = tuple(st.font.color.rgb)
                    break
        size = run.font.size.pt if run.font.size else None
        if size is None:
            for st in styles:
                if st.font.size:
                    size = st.font.size.pt
                    break
        shaded = False
        if rpr is not None:
            shd = rpr.find(f"{W}shd")
            hl = rpr.find(f"{W}highlight")
            shaded = (shd is not None and shd.get(W + "fill", "auto").lower() not in ("auto", "ffffff")) or (hl is not None and hl.get(W + "val") != "none")
        ppr_shd = paragraph._p.find(f"{W}pPr/{W}shd")
        if ppr_shd is not None and ppr_shd.get(W + "fill", "auto").lower() not in ("auto", "ffffff"):
            shaded = True
        for tc in r.iterancestors(f"{W}tc"):
            shd = tc.find(f"{W}tcPr/{W}shd")
            if shd is not None and shd.get(W + "fill", "auto").lower() not in ("auto", "ffffff"):
                shaded = True
        return reason, colour, size, shaded

    def reasons(self, run, paragraph) -> list[str]:
        vanish, colour, size, shaded = self.run_props(run, paragraph)
        out = [vanish] if vanish else []
        if colour is not None and not shaded and _lum_dist(colour, self.background) <= config.BG_MATCH_DIST:
            out.append("white_text")
        if size is not None and size <= config.DOCX_MIN_FONT_PT + config.FONT_SIZE_EPSILON:
            out.append("tiny_font")
        return out


def _heading_level(style) -> int | None:
    for st in _style_chain(style):
        name = (st.name or "").lower()
        m = re.fullmatch(r"heading (\d)", name)
        if m:
            return int(m.group(1))
        if name == "title":
            return 1
    return None


def _zip_precheck(path: Path) -> tuple[list[str], list[SecurityFinding], list[str]]:
    """(member names, security findings from the listing, truncation reasons). Bounds first, nothing parsed."""
    findings: list[SecurityFinding] = []
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        total = sum(i.file_size for i in infos)
        if len(infos) > config.DOCX_MAX_MEMBERS or total > config.DOCX_MAX_UNCOMPRESSED_MB * 1_048_576:
            return [], [], [f"zip holds {len(infos)} members / {total / 1_048_576:.0f} MB uncompressed, over the cap; nothing was parsed"]
        names = [i.filename for i in infos]
        for n in names:
            low = n.lower()
            if low.endswith("vbaproject.bin"):
                findings.append(SecurityFinding(detector="active_content", severity="high",
                                                evidence={"kind": "vba_macro", "member": n, "note": "macro project present; never executed"}))
            elif low.startswith("word/embeddings/") or low.startswith("word/activex/"):
                findings.append(SecurityFinding(detector="embedded_files", severity="medium",
                                                evidence={"name": n, "size": next(i.file_size for i in infos if i.filename == n), "source": "docx_member"}))
        for n in names:
            if n.endswith(".rels") and next(i.file_size for i in infos if i.filename == n) < 2_000_000:
                data = z.read(n)
                for m in _REL_EXTERNAL.finditer(data):
                    target = (m.group(1) or m.group(2) or b"").decode("utf-8", "replace")
                    if "attachedTemplate" in data[max(0, m.start() - 300):m.end() + 300].decode("utf-8", "replace"):
                        findings.append(SecurityFinding(detector="active_content", severity="high",
                                                        evidence={"kind": "remote_template", "target": target[:300], "member": n,
                                                                  "note": "attached template is loaded from a URL; never fetched"}))
    return names, findings, []


def _provenance(path: Path, doc) -> Provenance:
    cp = doc.core_properties
    total_time = None
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/app.xml" in z.namelist():
                m = re.search(rb"<TotalTime>(\d+)</TotalTime>", z.read("docProps/app.xml")[:200_000])
                total_time = int(m.group(1)) if m else None
    except (zipfile.BadZipFile, KeyError):
        pass

    def iso(d: datetime | None):
        return d.isoformat() if d else None

    created, modified = cp.created, cp.modified
    gap = (modified - created).total_seconds() / 86400 if created and modified else None
    creator, last = cp.author or None, cp.last_modified_by or None
    return Provenance(creator=creator, author=creator, created=iso(created), modified=iso(modified),
                      mod_minus_create_days=round(gap, 4) if gap is not None else None,
                      date_order_invalid=(gap < 0) if gap is not None else None,
                      last_modified_by=last, revision=cp.revision if cp.revision else None, total_editing_minutes=total_time,
                      creator_differs_from_last_modified_by=(creator != last) if creator and last else None,
                      producer=None)


def scan_docx(path: str | Path, max_seconds: float | None = None) -> DocxResult:
    from docx import Document
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run

    path = Path(path)
    res = DocxResult()
    deadline = time.monotonic() + (max_seconds or config.MAX_SECONDS)
    if path.stat().st_size > config.MAX_FILE_MB * 1_048_576:
        res.truncated = True
        res.truncation_reasons = [f"file is over the {config.MAX_FILE_MB} MB cap; nothing was parsed"]
        return res
    _names, res.findings, trunc = _zip_precheck(path)
    if trunc:
        res.truncated, res.truncation_reasons = True, trunc
        return res
    doc = Document(str(path))
    resolver = _Resolver(doc)
    res.provenance = _provenance(path, doc)

    units: list[structure.Unit] = []
    tables: list[Table] = []
    link_obs: list[LinkObs] = []
    visible_parts: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        if time.monotonic() > deadline:
            res.truncated = True
            res.truncation_reasons.append(f"time budget of {max_seconds or config.MAX_SECONDS:.0f}s reached")
            break
        if child.tag == f"{W}tbl":
            t = DocxTable(child, doc)
            rows = []
            for row in t.rows:
                cells = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:  # merged cell repeated by python-docx
                        continue
                    seen.add(id(cell._tc))
                    cells.append(" ".join(cell.text.split()))
                if any(cells):
                    rows.append(cells)
            if rows:
                tables.append(Table(page=1, strategy="docx", n_rows=len(rows), n_cols=max(len(r) for r in rows), rows=rows))
            continue
        if child.tag != f"{W}p":
            continue
        para = Paragraph(child, doc)
        vis: list[str] = []
        cur: list[tuple[str, list[str]]] = []  # hidden runs of this paragraph: (text, reasons)
        for r in child.iter(f"{W}r"):
            run = Run(r, para)
            text = run.text
            if not text.strip():
                vis.append(text)
                continue
            raw_reasons = resolver.reasons(run, para)
            if raw_reasons:
                if cur and cur[-1][1] == raw_reasons:
                    cur[-1] = (cur[-1][0] + text, raw_reasons)
                else:
                    cur.append((text, raw_reasons))
                continue
            vis.append(text)
        for text, reasons in cur:
            res.fragments.append(HiddenFragment(text=" ".join(text.split()), page=None, location_kind="docx_run", bbox=(0.0, 0.0, 0.0, 0.0), reasons=reasons,
                                                detection_method="docx_attribute"))
        for h in child.iter(f"{W}hyperlink"):
            rid = h.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            rel = doc.part.rels.get(rid) if rid else None
            if rel is not None and rel.is_external:
                shown = "".join(t.text or "" for t in h.iter(f"{W}t")).strip()
                link_obs.append(LinkObs(1, shown, "URI", rel.target_ref, linkcheck.compare(shown, "URI", rel.target_ref)))
        raw = "".join(vis)
        if has_hidden_unicode(raw):  # detect on RAW text before ftfy/cleaning erases the evidence
            for f in scan_text(raw):
                res.fragments.append(HiddenFragment(text=f.text, page=None, location_kind="docx_run", bbox=(0.0, 0.0, 0.0, 0.0), reasons=[f.kind],
                                                    detection_method="unicode"))
            raw = strip_hidden_unicode(raw)
        text, moji = hygiene.clean(" ".join(raw.split()))
        res.mojibake_fixed = res.mojibake_fixed or moji
        if not text:
            continue
        visible_parts.append(text)
        level = _heading_level(para.style)
        if level is not None and len(text) <= 200:
            h = Heading(text=text, level=level, page=1, size=None, method="style")
            units.append(structure.Unit("heading", text, 1, float(len(units)), 0.0, [SimpleNamespace(text=text)], h))
        else:
            units.append(structure.Unit("paragraph", text, 1, float(len(units)), 0.0, [SimpleNamespace(text=text)]))
    sections, full_text = structure.build_sections(units)
    block = StructureBlock(headings=[u.heading for u in units if u.heading], sections=sections, tables=tables, links=structure.build_links(link_obs, full_text),
                           numeric_claims=structure.numeric_claims(units, sections),
                           references=structure.references_block(units, sections, full_text), body_size=None, notes=[])
    res.extracted = structure.Extracted(block, full_text, units, {}, res.mojibake_fixed, [1])
    res.visible_text = "\n".join(visible_parts)
    res.findings += [f.model_copy(update={"page": None, "location_kind": "docx_run"}) for f in link_findings(link_obs)]
    return res
