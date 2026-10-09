"""Handling for untrusted text that is shown to a model.

Every piece of attacker-controllable text (hidden resume fragments, resume
text, repository content) must pass through ``wrap_untrusted`` before it is
placed in a prompt. The wrapper:

  * fences the text between delimiters carrying a fresh random nonce, so the
    text cannot forge the closing delimiter and "escape" the data region;
  * removes anything shaped like one of our delimiters from the text;
  * escapes invisible and format code points (zero-width, bidi controls,
    Unicode tag characters, ...) to visible \\uXXXX form, so smuggled
    characters cannot act on the model reading the prompt.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

UNTRUSTED_OPEN = "<<<UNTRUSTED_"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_"

_DELIMITER_LIKE = re.compile(r"<{2,}\s*(?:END_)?UNTRUSTED[^>]*>{0,3}", re.IGNORECASE)
_NONCE_BYTES = 8


def _escape_invisible(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cf", "Cc", "Co", "Cn", "Zl", "Zp") or 0xE0000 <= ord(ch) <= 0xE01EF:
            out.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        else:
            out.append(ch)
    return "".join(out)


def sanitize_untrusted(text: str) -> str:
    """Delimiter look-alikes removed and invisible code points made visible."""
    return _DELIMITER_LIKE.sub("[removed-delimiter]", _escape_invisible(text))


def wrap_untrusted(text: str, source: str = "untrusted") -> str:
    """Fence ``text`` as inert data for a prompt. A new nonce is drawn per call."""
    nonce = secrets.token_hex(_NONCE_BYTES)
    source = re.sub(r"[^a-z0-9_]", "", source.lower()) or "untrusted"
    return (
        f"{UNTRUSTED_OPEN}{nonce} source={source}>>>\n"
        f"{sanitize_untrusted(text)}\n"
        f"{UNTRUSTED_CLOSE}{nonce}>>>"
    )
