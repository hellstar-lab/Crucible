"""Geometric detectors for text a renderer never paints.

Render-and-diff (visibility.py) finds text that is DRAWN BUT INVISIBLE. It
cannot find text that is NEVER DRAWN, because that text changes no pixel
either way. A renderer paints only CropBox ∩ MediaBox, so text outside that
region is extractable but unseen. These detectors compare each character's box
with the page boxes directly.

Coordinates: pdfium reports character boxes in unrotated PDF user space with
y increasing upward, the same system as the page boxes (MediaBox, CropBox), so
no conversion is needed and /Rotate does not matter. pdfplumber must NOT be
used here: on a page whose MediaBox does not start at (0,0) it reports a
mirrored y range and an un-offset x range, and under /Rotate it swaps axes
(measured; see tests/test_doc_geometry.py).

Detectors
  offpage_text    chars outside the MediaBox (or outside CropBox ∩ MediaBox when no
                  gap exists): never painted on any medium.
  cropbox_shrunk  CropBox strictly smaller than the MediaBox with text in the gap:
                  cropping used to hide text.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.contracts import SecurityFinding

from . import config
from .extract import BBox, CharInfo

Box = tuple[float, float, float, float]  # (left, bottom, right, top), user space


@dataclass
class PageBoxes:
    media: Box
    crop: Box  # the declared CropBox (equals media when absent)
    effective: Box  # CropBox ∩ MediaBox: what a renderer paints
    degenerate: bool = False  # CropBox and MediaBox do not intersect


def normalise(box: tuple | list) -> Box:
    l, b, r, t = (float(v) for v in box)
    return (min(l, r), min(b, t), max(l, r), max(b, t))


def intersect(a: Box, b: Box) -> Box | None:
    l, bt, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (l, bt, r, t) if r > l and t > bt else None


def page_boxes(media: tuple | list, crop: tuple | list | None) -> PageBoxes:
    m = normalise(media)
    c = normalise(crop) if crop is not None else m
    eff = intersect(m, c)
    return PageBoxes(m, c, eff if eff else m, degenerate=eff is None)


def outside(bbox: BBox, box: Box, tol: float = config.OFFPAGE_TOLERANCE_PT) -> bool:
    """True when the whole char box lies beyond one edge of ``box`` by more than ``tol``."""
    l, b, r, t = bbox
    return r < box[0] - tol or l > box[2] + tol or t < box[1] - tol or b > box[3] + tol


def _measurable(c: CharInfo) -> bool:
    l, b, r, t = c.bbox
    return not c.is_generated and r > l and t > b


def _union(boxes: list[BBox]) -> list[float]:
    return [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
            round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]


def _finding(detector: str, page: int, flagged: list[CharInfo], boxes_violated: list[str], pb: PageBoxes) -> SecurityFinding:
    text = "".join(c.text for c in flagged)
    return SecurityFinding(
        detector=detector, severity="medium", page=page,
        evidence={"chars": len(flagged), "boxes_violated": boxes_violated, "bbox": _union([c.bbox for c in flagged]),
                  "mediabox": list(pb.media), "cropbox": list(pb.crop), "effective_box": list(pb.effective),
                  "tolerance_pt": config.OFFPAGE_TOLERANCE_PT, "degenerate_boxes": pb.degenerate,
                  "reconstructed_truncated": len(text) > config.MAX_RECONSTRUCTED_CHARS},
        reconstructed_text=text[: config.MAX_RECONSTRUCTED_CHARS])


def detect_offpage(page_no: int, chars: list[CharInfo], pb: PageBoxes) -> list[SecurityFinding]:
    """offpage_text and cropbox_shrunk for one page. Each char gets exactly one verdict."""
    beyond_media: list[CharInfo] = []  # outside the MediaBox: never painted anywhere
    in_gap: list[CharInfo] = []  # inside the MediaBox but outside the CropBox
    for c in chars:
        if not _measurable(c) or not outside(c.bbox, pb.effective):
            continue
        (beyond_media if outside(c.bbox, pb.media) else in_gap).append(c)
    out: list[SecurityFinding] = []
    if beyond_media:
        boxes = ["mediabox"] + (["cropbox"] if any(outside(c.bbox, pb.crop) for c in beyond_media) else [])
        out.append(_finding("offpage_text", page_no, beyond_media, sorted(boxes), pb))
    if in_gap:
        out.append(_finding("cropbox_shrunk", page_no, in_gap, ["cropbox"], pb))
    return out
