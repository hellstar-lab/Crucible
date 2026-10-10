"""Font inventory and the font_anomaly detector.

font_anomaly reports a MID-LINE change of font family inside body text as
``possible_pasted_text``. It is a low-severity FACT, never an accusation and never
a status change: legitimate documents mix families on one line (logos, icon fonts,
inline code, designed headers). To keep it quiet, symbol/icon/math fonts and
monospace are ignored, a run needs at least FONT_RUN_MIN_ALNUM letters or digits to
count, only body-size lines are examined, and a change never crosses a column gap.
"""

from __future__ import annotations

from backend.contracts import SecurityFinding

from .lines import SEGMENT_GAP_EM, SEGMENT_GAP_MIN_PT, font_family, is_mono_family, is_symbol_family, strip_subset

FONT_RUN_MIN_ALNUM = 4
BODY_SIZE_TOLERANCE_PT = 1.0
MAX_LINES_PER_FINDING = 10


def _segments(runs, size: float):
    seg, prev = [], None
    for r in sorted(runs, key=lambda r: r.x0):
        if prev is not None and r.x0 - prev.x1 > max(SEGMENT_GAP_EM * size, SEGMENT_GAP_MIN_PT):
            yield seg
            seg = []
        seg.append(r)
        prev = r
    if seg:
        yield seg


def _alnum(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def detect_font_anomaly(lines_by_page: dict[int, list], body_by_page: dict[int, float | None]) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    for page, lines in sorted(lines_by_page.items()):
        body = body_by_page.get(page)
        hits = []
        for ln in lines:
            if body is None or abs(ln.size - body) > BODY_SIZE_TOLERANCE_PT:
                continue
            for seg in _segments(ln.runs, ln.size):
                sig = [r for r in seg if r.family and not is_symbol_family(r.family) and not is_mono_family(r.family)
                       and _alnum(r.text) >= FONT_RUN_MIN_ALNUM]
                fams = {r.family for r in sig}
                if len(fams) >= 2:
                    weight = {f: sum(_alnum(r.text) for r in sig if r.family == f) for f in fams}
                    dominant = max(weight, key=weight.get)
                    hits.append({"line": ln.text[:120], "families": sorted(fams), "dominant": dominant,
                                 "inserted": [r.text for r in sig if r.family != dominant][:5]})
        if hits:
            inserted = [t for h in hits for t in h["inserted"]]
            out.append(SecurityFinding(
                detector="font_anomaly", severity="low", page=page,
                evidence={"kind": "possible_pasted_text", "lines": hits[:MAX_LINES_PER_FINDING], "count": len(hits),
                          "note": "a fact about mixed fonts on one line, not an accusation"},
                reconstructed_text=" | ".join(inserted)[:2000]))
    return out


def font_inventory(reader, pages: list) -> list[dict]:
    """Per page: fonts actually used (from characters), subset prefix, and whether the font program is embedded."""
    embedded_by_name: dict[str, bool] = {}
    for i, page in enumerate(reader.pages[: len(pages)]):
        fonts = (page.get("/Resources") or {}).get("/Font") if page.get("/Resources") is not None else None
        for ref in (fonts or {}).values():
            try:
                f = ref.get_object()
                name = str(f.get("/BaseFont", "")).lstrip("/")
                sub = str(f.get("/Subtype", ""))
                target = f
                if sub == "/Type0":
                    target = f["/DescendantFonts"][0].get_object()
                desc = target.get("/FontDescriptor")
                desc = desc.get_object() if desc is not None else {}
                embedded_by_name[name] = sub == "/Type3" or any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            except Exception:  # noqa: BLE001 - a malformed font dict is "unknown", not an error for the document
                continue
    out = []
    for pd in pages:
        counts: dict[str, int] = {}
        for c in pd.chars:
            if c.font_name and c.text.strip():
                counts[c.font_name] = counts.get(c.font_name, 0) + 1
        out.append({"page": pd.page, "fonts": [
            {"name": n, "subset_prefix": n.split("+")[0] if n[6:7] == "+" and n[:6].isupper() else None,
             "family": font_family(n), "embedded": embedded_by_name.get(n, embedded_by_name.get(strip_subset(n))), "chars": k}
            for n, k in sorted(counts.items(), key=lambda kv: -kv[1])]})
    return out
