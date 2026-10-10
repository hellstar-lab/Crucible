"""Text hygiene for extraction: ftfy repairs mojibake and normalises ligatures.

ORDER MATTERS. ftfy erases exactly the evidence the Unicode detectors look for
(it strips a BOM, and normalises width and ligature characters), so:

    1. detect on RAW text   (unicode_scan, run per page inside Shield)
    2. only then clean the text that downstream stages will read (this module)

Raw text is always kept; cleaning never feeds detection. Curly quotes are
preserved so verbatim contexts still match the document.
"""

from __future__ import annotations

import ftfy
from ftfy import TextFixerConfig

_CONFIG = TextFixerConfig(uncurl_quotes=False)
_MOJIBAKE_PARAMS = {"decode_inconsistent_utf8", "restore_byte_a0", "replace_lossy_sequences", "fix_c1_controls"}


def clean(raw: str) -> tuple[str, bool]:
    """(cleaned text, whether a MOJIBAKE repair was needed). Ligature, width and entity
    normalisations change the text but do not count as mojibake."""
    if raw.isascii():
        return raw, False
    fixed, steps = ftfy.fix_and_explain(raw, _CONFIG)
    mojibake = any(s[0] in ("encode", "decode") or s[1] in _MOJIBAKE_PARAMS for s in (steps or []))
    return fixed, mojibake
