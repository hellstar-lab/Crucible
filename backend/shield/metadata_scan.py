"""Flag overlong PDF metadata, a common hiding place for injected text.

Technique caught: instructions stuffed into Title/Subject/Keywords or the XMP
packet, which no human sees in a PDF viewer but text pipelines often ingest.
The Info dictionary is read with pypdfium2; XMP (which pdfium does not
expose) is read with pdfplumber's underlying pdfminer document.
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdftypes import resolve1

from . import config

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]*>")
_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")


@dataclass
class MetadataFinding:
    field: str  # Info key, or "xmp"
    value: str
    source: str  # "info" | "xmp"


def readable_payload(value: str) -> str | None:
    """The text a metadata value carries, or None if it is a binary blob.

    XMP routinely embeds base64 JPEG thumbnails, which are long but harmless.
    A value that is pure base64 is decoded: binary content is ignored, while
    decoded readable text is returned so base64-wrapped instructions are
    still caught.
    """
    compact = "".join(value.split())
    if " " in value.strip() or not _B64.fullmatch(compact) or len(compact) % 4:
        return value
    try:
        decoded = base64.b64decode(compact, validate=True)
        text = decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None if len(compact) > config.MAX_METADATA_LEN else value
    printable = sum(ch.isprintable() or ch.isspace() for ch in text)
    return f"{value} [base64 decoded: {text}]" if text and printable / len(text) >= config.B64_TEXT_PRINTABLE_MIN else None


def scan_info(pdf: pdfium.PdfDocument) -> list[MetadataFinding]:
    """Flag Info-dictionary fields longer than MAX_METADATA_LEN characters."""
    out = []
    for key, value in pdf.get_metadata_dict(skip_empty=True).items():
        payload = readable_payload(value)
        if payload is not None and len(value) > config.MAX_METADATA_LEN:
            out.append(MetadataFinding(key, payload, "info"))
    return out


def xmp_segments(xmp: str) -> list[str]:
    """Text content of an XMP packet, split on tags. A regex is used instead
    of an XML parser so a hostile packet cannot trigger entity expansion."""
    return [html.unescape(s).strip() for s in _TAG.split(xmp) if s.strip()]


def scan_xmp(path: Path) -> list[MetadataFinding]:
    """Flag XMP text segments longer than MAX_METADATA_LEN characters.

    Best-effort: if pdfminer cannot read the file, log and return nothing —
    pdfium has already accepted the document by the time this runs.
    """
    try:
        with pdfplumber.open(path) as plumber:
            ref = plumber.doc.catalog.get("Metadata")
            if ref is None:
                return []
            raw = resolve1(ref).get_data()
    except Exception as exc:  # noqa: BLE001 - third-party parser on untrusted input
        log.warning("XMP scan skipped for %s: %s", path.name, exc)
        return []
    text = raw.decode("utf-8", errors="replace")
    out = []
    for seg in xmp_segments(text):
        if len(seg) <= config.MAX_METADATA_LEN:
            continue
        payload = readable_payload(seg)
        if payload is None:
            log.debug("XMP: skipped %d-char binary base64 segment", len(seg))
            continue
        out.append(MetadataFinding("xmp", payload, "xmp"))
    return out


def scan_metadata(pdf: pdfium.PdfDocument, path: Path) -> list[MetadataFinding]:
    return scan_info(pdf) + scan_xmp(path)
