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

Nonce (Phase 4A, S4). A random nonce made temperature-0 runs irreproducible (measured: the same eval row
passed 3/5 and failed 2/5 with identical prompts apart from the nonce). So when a key is configured the nonce is

    nonce = HMAC-SHA256(key, source_label + "\\x00" + text), first NONCE_HEX hex characters

The attacker knows the text but not the key, so the closing delimiter stays unguessable, and the same input
always gets the same nonce, so runs stay repeatable. A document cannot contain its own delimiter either: the
nonce depends on the full text, so any edit that inserts a delimiter changes the nonce it would need.

The key comes ONLY from the environment (CRUCIBLE_NONCE_KEY, at least 256 bits as hex), is generated once
(``python -c "import secrets; print(secrets.token_hex(32))"``), is never stored in the repository and must not be
derived from any API key. When it is absent or invalid the nonce falls back to RANDOM, never to a constant:
losing reproducibility is acceptable, losing unpredictability is not. The active mode is logged once.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata

log = logging.getLogger(__name__)

UNTRUSTED_OPEN = "<<<UNTRUSTED_"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_"

_DELIMITER_LIKE = re.compile(r"<{2,}\s*(?:END_)?UNTRUSTED[^>]*>{0,3}", re.IGNORECASE)
_NONCE_BYTES = 8
NONCE_HEX = 16  # >= 16 hex characters in both modes
NONCE_KEY_ENV = "CRUCIBLE_NONCE_KEY"
MIN_KEY_BYTES = 32  # 256 bits
_logged_modes: set[str] = set()


def _nonce_key() -> bytes | None:
    """The HMAC key from the environment, or None (absent, not hex, shorter than 256 bits, or equal to an API key)."""
    raw = os.environ.get(NONCE_KEY_ENV)
    if not raw:
        return None
    raw = raw.strip()
    if any(raw == (os.environ.get(k) or "").strip() for k in ("NEBIUS_API_KEY", "GITHUB_TOKEN")):
        _log_mode("random", f"{NONCE_KEY_ENV} equals an API key and is ignored")
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        _log_mode("random", f"{NONCE_KEY_ENV} is not hex and is ignored")
        return None
    if len(key) < MIN_KEY_BYTES:
        _log_mode("random", f"{NONCE_KEY_ENV} is shorter than 256 bits and is ignored")
        return None
    return key


def _log_mode(mode: str, why: str) -> None:
    if (mode, why) not in _logged_modes:
        _logged_modes.add((mode, why))
        (log.warning if "ignored" in why else log.info)("wrap_untrusted nonce mode: %s (%s)", mode, why)


def nonce_mode() -> str:
    return "hmac" if _nonce_key() is not None else "random"


def _nonce(text: str, source: str) -> str:
    key = _nonce_key()
    if key is None:
        _log_mode("random", f"{NONCE_KEY_ENV} not set: runs are not reproducible")
        return secrets.token_hex(_NONCE_BYTES)[:NONCE_HEX]
    _log_mode("hmac", "deterministic per (source, text)")
    return hmac.new(key, f"{source}\x00{text}".encode("utf-8"), hashlib.sha256).hexdigest()[:NONCE_HEX]


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
    """Fence ``text`` as inert data for a prompt. The nonce is HMAC(key, source, text) when a key is configured,
    otherwise random per call (see the module docstring)."""
    source = re.sub(r"[^a-z0-9_]", "", source.lower()) or "untrusted"
    nonce = _nonce(text, source)
    return (
        f"{UNTRUSTED_OPEN}{nonce} source={source}>>>\n"
        f"{sanitize_untrusted(text)}\n"
        f"{UNTRUSTED_CLOSE}{nonce}>>>"
    )
