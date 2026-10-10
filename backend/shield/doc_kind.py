"""Per-page document kind. Cheap, deterministic, never one document-level guess.

  image_only  fewer than KIND_IMAGE_MAX_CHARS chars AND an image  -> needs OCR; text fields are None
  blank       fewer than KIND_IMAGE_MAX_CHARS chars and no image
  slide_deck  landscape page, body text >= SLIDE_MIN_BODY_SIZE, at most SLIDE_MAX_CHARS chars
  text_pdf    at least KIND_TEXT_MIN_CHARS chars
  sparse      anything in between (title pages, short pages)

A document is "mixed" simply when its pages differ; ``summary`` counts them.
"""

from __future__ import annotations

import statistics
from collections import Counter

from . import config


def body_size(chars) -> float | None:
    sizes = [round(c.font_size * 2) / 2 for c in chars if c.text.strip() and not c.is_generated and c.font_size > 0]
    return Counter(sizes).most_common(1)[0][0] if sizes else None


def classify_page(pd) -> dict:
    # "chars per page" as pdfplumber counts it: every real glyph incl. literal spaces, not pdfium's synthetic ones
    shown = [c for c in pd.chars if not c.is_generated and c.text.isprintable() and c.index not in pd.hidden]
    n = len(shown)
    body = body_size(shown)
    landscape = pd.width > pd.height
    if n < config.KIND_IMAGE_MAX_CHARS:
        kind = "image_only" if pd.images > 0 else "blank"
    elif landscape and body is not None and body >= config.SLIDE_MIN_BODY_SIZE and n <= config.SLIDE_MAX_CHARS:
        kind = "slide_deck"
    elif n >= config.KIND_TEXT_MIN_CHARS:
        kind = "text_pdf"
    else:
        kind = "sparse"
    return {"page": pd.page, "kind": kind, "chars": n, "images": pd.images, "width": round(pd.width, 1),
            "height": round(pd.height, 1), "body_size": body}


def summary(per_page: list[dict]) -> dict:
    return dict(Counter(p["kind"] for p in per_page))


def median_size(chars) -> float | None:
    sizes = [c.font_size for c in chars if c.text.strip() and c.font_size > 0]
    return statistics.median(sizes) if sizes else None
