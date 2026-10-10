"""pypdf-based document-level detectors. Each is a named, separately testable function.

Hostile-input rules for everything here: never execute embedded content, never
follow a URL found in a document, never open or parse an attachment. Attachment
bytes are only sniffed (size and the first 8 bytes), with a bounded decompress so a
compression bomb cannot be expanded. A detector that raises is recorded by the
caller as ``needs_review``, never swallowed into "clean".

  link_findings        link_target_mismatch   display text names one host, the target is another
  detect_embedded      embedded_files         /Names /EmbeddedFiles and FileAttachment annotations
  detect_active        active_content         /OpenAction (non-view), /AA, /JavaScript, /Launch, /RichMedia, /XFA;
                                              AcroForm and signature fields are reported as info
  extract_provenance   (provenance block)     Info + XMP: producer, creator, author, created, modified
  detect_revisions     revision_history       incremental updates (several %%EOF and a /Prev chain)
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, PdfObject

from backend.contracts import Provenance, SecurityFinding

from . import config, linkcheck
from .lines import text_from_chars

MAX_ANNOTS_PER_PAGE = 2000
MAX_ATTACHMENTS = 200
MAX_NAME_TREE_NODES = 500
SNIFF_BYTES = 8
JS_EXCERPT_CHARS = 200
MAX_ACTION_CHAIN = 5

DANGEROUS_ACTIONS = {"JavaScript", "Launch", "URI", "SubmitForm", "ImportData", "GoToR", "Rendition", "Movie", "Sound"}
BENIGN_ACTIONS = {"GoTo", "Named", "GoToE", "Hide", "SetOCGState", "ResetForm"}


def _obj(o):
    return o.get_object() if isinstance(o, IndirectObject) else o


def _s(o) -> str | None:
    o = _obj(o)
    if o is None:
        return None
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


def open_reader(path: str | Path) -> PdfReader:
    return PdfReader(str(path), strict=False)


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


def action_chain(action) -> list[DictionaryObject]:
    """The action and its /Next successors (bounded; /Next may be an action or an array)."""
    out: list[DictionaryObject] = []
    todo = [_obj(action)]
    while todo and len(out) < MAX_ACTION_CHAIN:
        a = todo.pop(0)
        if isinstance(a, DictionaryObject):
            out.append(a)
            nxt = _obj(a.get("/Next"))
            if isinstance(nxt, ArrayObject):
                todo.extend(_obj(x) for x in nxt)
            elif nxt is not None:
                todo.append(nxt)
    return out


def _file_spec(fs) -> str | None:
    fs = _obj(fs)
    if isinstance(fs, DictionaryObject):
        return _s(fs.get("/UF") or fs.get("/F") or (_obj(fs.get("/Win")) or {}).get("/F"))
    return _s(fs)


def action_target(act: DictionaryObject) -> tuple[str, str | None]:
    kind = str(act.get("/S", "")).lstrip("/")
    if kind == "URI":
        return "URI", _s(act.get("/URI"))
    if kind in ("Launch", "GoToR"):
        return kind, _file_spec(act.get("/F") or act.get("/Win"))
    if kind == "SubmitForm":
        return "Submit", _file_spec(act.get("/F"))
    return kind, None


def _js_text(act: DictionaryObject) -> str:
    js = _obj(act.get("/JS"))
    if hasattr(js, "get_data"):
        return js.get_data().decode("utf-8", "replace")
    return _s(js) or ""


# --------------------------------------------------------------------------
# link_target_mismatch
# --------------------------------------------------------------------------


@dataclass
class LinkObs:
    page: int
    display_text: str
    action: str  # URI | Launch | GoToR | Submit | JavaScript | GoTo ...
    target: str | None
    mismatch: dict | None


def _rect(annot) -> tuple[float, float, float, float] | None:
    r = _obj(annot.get("/Rect"))
    if not r or len(r) < 4:
        return None
    try:
        a, b, c, d = (float(_obj(v)) for v in r[:4])
    except (TypeError, ValueError):
        return None
    return (min(a, c), min(b, d), max(a, c), max(b, d))


def link_observations(reader: PdfReader, pages: list) -> list[LinkObs]:
    """Every /Link annotation with the words printed inside its rectangle. ``pages`` are
    Shield PageData (character boxes in the same user space as /Rect)."""
    by_page = {pd.page: pd for pd in pages}
    out: list[LinkObs] = []
    for i, page in enumerate(reader.pages[: len(by_page) or config.MAX_PAGES], start=1):
        annots = _obj(page.get("/Annots"))
        if not annots:
            continue
        pd = by_page.get(i)
        for ref in list(annots)[:MAX_ANNOTS_PER_PAGE]:
            a = _obj(ref)
            if not isinstance(a, DictionaryObject) or str(a.get("/Subtype", "")) != "/Link":
                continue
            rect = _rect(a)
            display = ""
            if rect and pd is not None:
                l, b, r, t = rect[0] - 1, rect[1] - 1, rect[2] + 1, rect[3] + 1
                inside = [c for c in pd.chars if l <= (c.bbox[0] + c.bbox[2]) / 2 <= r and b <= (c.bbox[1] + c.bbox[3]) / 2 <= t]
                display = text_from_chars(inside, pd)
            actions = action_chain(a.get("/A")) if a.get("/A") is not None else []
            if not actions:
                continue  # internal destination only
            for act in actions:
                kind, target = action_target(act)
                if kind in ("URI", "Launch", "GoToR", "Submit"):
                    out.append(LinkObs(i, display, kind, target, linkcheck.compare(display, kind, target)))
    return out


def link_findings(obs: list[LinkObs]) -> list[SecurityFinding]:
    return [SecurityFinding(detector="link_target_mismatch", severity="high", page=o.page,
                            evidence={"display_text": o.display_text[:200], **o.mismatch}, reconstructed_text=o.display_text[:200])
            for o in obs if o.mismatch]


# --------------------------------------------------------------------------
# embedded_files
# --------------------------------------------------------------------------


def _walk_name_tree(node, depth: int = 0, budget: list[int] | None = None):
    budget = budget if budget is not None else [MAX_NAME_TREE_NODES]
    node = _obj(node)
    if not isinstance(node, DictionaryObject) or depth > 12 or budget[0] <= 0:
        return
    budget[0] -= 1
    names = _obj(node.get("/Names"))
    if isinstance(names, ArrayObject):
        for i in range(0, len(names) - 1, 2):
            yield _s(names[i]), _obj(names[i + 1])
    for kid in _obj(node.get("/Kids")) or []:
        yield from _walk_name_tree(kid, depth + 1, budget)


def _sniff(stream) -> tuple[int | None, str | None, int | None, str | None]:
    """(declared_size, first8_hex, stored_bytes, note) WITHOUT expanding the stream beyond 8 bytes."""
    stream = _obj(stream)
    if stream is None:
        return None, None, None, "no embedded stream"
    params = _obj(stream.get("/Params")) or {}
    declared = _obj(params.get("/Size")) if isinstance(params, DictionaryObject) else None
    raw = getattr(stream, "_data", None)
    if raw is None:
        return (int(declared) if declared is not None else None), None, None, "stream data unavailable"
    filt = _obj(stream.get("/Filter"))
    filters = [str(f) for f in (filt if isinstance(filt, ArrayObject) else [filt] if filt else [])]
    try:
        if not filters:
            head = bytes(raw[:SNIFF_BYTES])
        elif filters == ["/FlateDecode"]:
            head = zlib.decompressobj().decompress(bytes(raw), SNIFF_BYTES)  # bounded: cannot expand a bomb
        else:
            return (int(declared) if declared is not None else None), None, len(raw), f"filter {filters} not sniffed"
    except zlib.error:
        return (int(declared) if declared is not None else None), None, len(raw), "stream could not be decompressed"
    return (int(declared) if declared is not None else len(raw) if not filters else None), head.hex(), len(raw), None


def detect_embedded(reader: PdfReader) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    root = reader.trailer["/Root"]
    seen = 0

    def add(name, fs, source, page=None):
        nonlocal seen
        seen += 1
        if seen > MAX_ATTACHMENTS or not isinstance(fs, DictionaryObject):
            return
        ef = _obj(fs.get("/EF")) or {}
        stream = _obj(ef.get("/F") or ef.get("/UF")) if isinstance(ef, DictionaryObject) else None
        size, first8, stored, note = _sniff(stream)
        out.append(SecurityFinding(
            detector="embedded_files", severity="medium", page=page,
            evidence={"name": (_file_spec(fs) or name or "")[:200], "size": size, "stored_bytes": stored, "first8_hex": first8,
                      "source": source, **({"note": note} if note else {})}))

    names = _obj(root.get("/Names"))
    if isinstance(names, DictionaryObject) and "/EmbeddedFiles" in names:
        for name, fs in _walk_name_tree(names["/EmbeddedFiles"]):
            add(name, fs, "names_tree")
    for i, page in enumerate(reader.pages, start=1):
        for ref in list(_obj(page.get("/Annots")) or [])[:MAX_ANNOTS_PER_PAGE]:
            a = _obj(ref)
            if isinstance(a, DictionaryObject) and str(a.get("/Subtype", "")) == "/FileAttachment":
                add(None, _obj(a.get("/FS")), "file_attachment_annotation", i)
    return out


# --------------------------------------------------------------------------
# active_content
# --------------------------------------------------------------------------


def detect_active(reader: PdfReader) -> tuple[list[SecurityFinding], dict]:
    """(findings, facts). facts: has_form, acroform_fields. One finding per KIND with a count."""
    hits: dict[str, list[dict]] = {}

    def hit(kind: str, **ev):
        hits.setdefault(kind, []).append(ev)

    root = reader.trailer["/Root"]
    oa = _obj(root.get("/OpenAction"))
    if isinstance(oa, DictionaryObject):  # an array is a view destination: benign
        for act in action_chain(oa):
            s = str(act.get("/S", "")).lstrip("/")
            if s in DANGEROUS_ACTIONS:
                hit("open_action", action=s, excerpt=_js_text(act)[:JS_EXCERPT_CHARS] if s == "JavaScript" else None)

    def aa(container, where):
        d = _obj(container.get("/AA")) if isinstance(container, DictionaryObject) else None
        if isinstance(d, DictionaryObject):
            for event, act in d.items():
                for a in action_chain(act):
                    s = str(a.get("/S", "")).lstrip("/")
                    if s not in BENIGN_ACTIONS:
                        hit("additional_actions", where=where, event=str(event).lstrip("/"), action=s,
                            excerpt=_js_text(a)[:JS_EXCERPT_CHARS] if s == "JavaScript" else None)

    aa(root, "document")
    names = _obj(root.get("/Names"))
    if isinstance(names, DictionaryObject) and "/JavaScript" in names:
        for name, act in _walk_name_tree(names["/JavaScript"]):
            hit("names_javascript", name=name, excerpt=_js_text(act)[:JS_EXCERPT_CHARS] if isinstance(act, DictionaryObject) else None)

    for i, page in enumerate(reader.pages, start=1):
        aa(page, f"page {i}")
        for ref in list(_obj(page.get("/Annots")) or [])[:MAX_ANNOTS_PER_PAGE]:
            a = _obj(ref)
            if not isinstance(a, DictionaryObject):
                continue
            sub = str(a.get("/Subtype", "")).lstrip("/")
            aa(a, f"page {i} annotation")
            if sub == "RichMedia":
                hit("rich_media", page=i)
            for act in action_chain(a.get("/A")) if a.get("/A") is not None else []:
                s = str(act.get("/S", "")).lstrip("/")
                if s == "Launch":
                    hit("launch_action", page=i, target=(_file_spec(act.get("/F") or act.get("/Win")) or "")[:200])
                elif s == "JavaScript":
                    hit("annotation_javascript", page=i, excerpt=_js_text(act)[:JS_EXCERPT_CHARS])

    facts = {"has_form": False, "acroform_fields": 0}
    form = _obj(root.get("/AcroForm"))
    if isinstance(form, DictionaryObject):
        fields = _obj(form.get("/Fields")) or []
        n_fields, n_sig = 0, 0
        stack = list(fields)
        while stack and n_fields < 5000:
            f = _obj(stack.pop())
            if isinstance(f, DictionaryObject):
                kids = _obj(f.get("/Kids"))
                if kids:
                    stack.extend(kids)
                else:
                    n_fields += 1
                    if str(f.get("/FT", "")) == "/Sig":
                        n_sig += 1
        facts = {"has_form": n_fields > 0, "acroform_fields": n_fields}
        if n_fields:
            hit("acroform", fields=n_fields)
        if n_sig:
            hit("signature_field", fields=n_sig)
        if "/XFA" in form:
            hit("xfa")

    severity = {"open_action": "high", "names_javascript": "high", "launch_action": "high", "annotation_javascript": "high",
                "additional_actions": "medium", "rich_media": "medium", "xfa": "medium", "acroform": "info",
                "signature_field": "info"}
    findings = []
    for kind, items in hits.items():
        sev = severity[kind]
        if kind == "additional_actions" and any(i.get("action") == "JavaScript" for i in items):
            sev = "high"
        findings.append(SecurityFinding(
            detector="active_content", severity=sev, page=None,
            evidence={"kind": kind, "count": len(items), "items": items[:10]},
            reconstructed_text=next((i["excerpt"] for i in items if i.get("excerpt")), None)))
    return findings, facts


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

_PDF_DATE = re.compile(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:(Z)|([+-])(\d{2})(?:'?(\d{2})'?)?)?'?$")


def parse_pdf_date(value) -> datetime | None:
    """D:YYYYMMDDHHmmSSOHH'mm' -> aware datetime, or None. Never guesses: any malformed or
    out-of-range string is None. Missing time parts take the PDF-spec defaults; a missing
    zone is taken as UTC."""
    if value is None:
        return None
    m = _PDF_DATE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) if g else dflt for g, dflt in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0)))
    try:
        tz = timezone.utc
        if m.group(8):
            delta = timedelta(hours=int(m.group(9)), minutes=int(m.group(10) or 0))
            tz = timezone(delta if m.group(8) == "+" else -delta)
        return datetime(y, mo, d, h, mi, s, tzinfo=tz)
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def extract_provenance(reader: PdfReader) -> Provenance:
    meta = reader.metadata or {}
    created = parse_pdf_date(meta.get("/CreationDate"))
    modified = parse_pdf_date(meta.get("/ModDate"))
    gap = (modified - created).total_seconds() / 86400 if created and modified else None
    xc = xm = None
    try:
        x = reader.xmp_metadata
        if x is not None:
            xc, xm = getattr(x, "xmp_create_date", None), getattr(x, "xmp_modify_date", None)
    except Exception:  # noqa: BLE001 - malformed XMP is recorded as absent, never trusted
        pass
    return Provenance(
        producer=_s(meta.get("/Producer")), creator=_s(meta.get("/Creator")), author=_s(meta.get("/Author")),
        created=_iso(created), modified=_iso(modified),
        mod_minus_create_days=round(gap, 4) if gap is not None else None,
        date_order_invalid=(gap < 0) if gap is not None else None,
        xmp_created=_iso(xc) if isinstance(xc, datetime) else _s(xc), xmp_modified=_iso(xm) if isinstance(xm, datetime) else _s(xm))


# --------------------------------------------------------------------------
# revision_history
# --------------------------------------------------------------------------

_EOF = re.compile(rb"%%EOF")


def detect_revisions(path: str | Path) -> list[SecurityFinding]:
    """Incremental updates. A file saved incrementally keeps its original bytes and appends a new
    body, xref section and trailer (with /Prev) ending in another %%EOF. A marker counts only when
    it follows a startxref and is followed by end-of-file or by the next body (an object, an xref
    or a new header), so a %%EOF inside an embedded file or stream is not counted. Linearized
    files carry two sections without having been edited; they are excluded.

    Verified against a REAL incremental save (tests/fixtures/docs/revision_incremental.pdf):
    original bytes preserved, two %%EOF, a /Prev chain.
    """
    data = Path(path).read_bytes()
    markers = []
    for m in _EOF.finditer(data):
        before = data[max(0, m.start() - 48):m.start()]
        after = data[m.end():m.end() + 40].lstrip()
        if b"startxref" in before and (not after or re.match(rb"\d+\s+\d+\s+obj|xref|%PDF", after)):
            markers.append(m.start())
    linearized = b"/Linearized" in data[:1024]
    prev_links = len(re.findall(rb"/Prev\s+\d+", data))
    if linearized or len(markers) < 2:
        return []
    return [SecurityFinding(
        detector="revision_history", severity="low", page=None,
        evidence={"revision_count": len(markers), "eof_offsets": markers[:20], "prev_links": prev_links,
                  "bytes_after_first_revision": len(data) - (markers[0] + 5),
                  "note": "the file was edited and re-saved incrementally after it was first written"})]
