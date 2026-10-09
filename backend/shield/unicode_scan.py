"""Scan extracted text for invisible or deceptive Unicode.

These characters draw nothing (or reorder what is drawn), so a human reading
the page never sees them while a model reading the text layer does.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config

TAG_RANGE = range(0xE0000, 0xE0080)
TAG_CANCEL = 0xE007F
BIDI_INITIATORS = frozenset([0x202A, 0x202B, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068])
BIDI_POPS = frozenset([0x202C, 0x2069])
DEPRECATED_FORMAT = range(0x206A, 0x2070)
ZERO_WIDTH = frozenset(
    [0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
     0x180E, 0x115F, 0x1160, 0x3164, 0xFFA0]
)
VARIATION_SELECTORS = frozenset(list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)))


@dataclass
class UnicodeFinding:
    start: int  # [start, end) into the scanned string
    end: int
    kind: str
    raw: str
    decoded: str | None = None  # tag characters decoded back to ASCII

    @property
    def text(self) -> str:
        """Human-readable form: the decoded payload when there is one."""
        return self.decoded if self.decoded is not None else self.raw


def decode_tags(s: str) -> str:
    """Map Unicode tag characters back to the ASCII they shadow."""
    return "".join(chr(ord(ch) - 0xE0000) for ch in s if ord(ch) in TAG_RANGE and ord(ch) != TAG_CANCEL)


def _runs(text: str, members) -> list[tuple[int, int]]:
    runs, i = [], 0
    while i < len(text):
        if ord(text[i]) in members:
            j = i
            while j < len(text) and ord(text[j]) in members:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def scan_tags(text: str) -> list[UnicodeFinding]:
    """Technique: ASCII smuggling — instructions written in invisible Unicode
    tag characters (U+E0000–U+E007F). Any occurrence fires; the payload is
    decoded back to readable ASCII."""
    return [
        UnicodeFinding(s, e, "unicode_tag_chars", text[s:e], decode_tags(text[s:e]))
        for s, e in _runs(text, TAG_RANGE)
    ]


def scan_bidi(text: str) -> list[UnicodeFinding]:
    """Technique: bidirectional overrides/isolates that make text display in a
    different order than it is read (Trojan Source style). Any occurrence
    fires; the finding spans from the control to its pop or end of line."""
    out, i = [], 0
    while i < len(text):
        cp = ord(text[i])
        if cp in BIDI_INITIATORS:
            j = i + 1
            while j < len(text) and text[j] not in "\r\n" and ord(text[j]) not in BIDI_POPS:
                j += 1
            if j < len(text) and ord(text[j]) in BIDI_POPS:
                j += 1
            out.append(UnicodeFinding(i, j, "bidi_control", text[i:j]))
            i = j
        elif cp in BIDI_POPS:
            out.append(UnicodeFinding(i, i + 1, "bidi_control", text[i]))
            i += 1
        else:
            i += 1
    return out


def scan_deprecated_format(text: str) -> list[UnicodeFinding]:
    """Technique: deprecated format characters (U+206A–U+206F) that have no
    legitimate use in a modern resume. Any occurrence fires."""
    return [UnicodeFinding(s, e, "deprecated_format_char", text[s:e]) for s, e in _runs(text, DEPRECATED_FORMAT)]


def scan_zero_width(text: str) -> list[UnicodeFinding]:
    """Technique: zero-width / filler steganography — data encoded in runs of
    invisible characters. Single ones are common copy-paste residue, so only
    runs of MIN_ZW_RUN or more fire."""
    return [
        UnicodeFinding(s, e, "zero_width_run", text[s:e])
        for s, e in _runs(text, ZERO_WIDTH)
        if e - s >= config.MIN_ZW_RUN
    ]


def scan_variation_selectors(text: str) -> list[UnicodeFinding]:
    """Technique: data smuggled in variation selectors. Emoji legitimately use
    one or two, so only runs of MIN_VS_RUN or more fire."""
    return [
        UnicodeFinding(s, e, "variation_selector_run", text[s:e])
        for s, e in _runs(text, VARIATION_SELECTORS)
        if e - s >= config.MIN_VS_RUN
    ]


def scan_text(text: str) -> list[UnicodeFinding]:
    """Run every Unicode scanner over ``text``; findings sorted by position."""
    findings = (
        scan_tags(text)
        + scan_bidi(text)
        + scan_deprecated_format(text)
        + scan_zero_width(text)
        + scan_variation_selectors(text)
    )
    return sorted(findings, key=lambda f: (f.start, f.end))


def strip_hidden_unicode(text: str) -> str:
    """Remove invisible/deceptive code points so downstream stages read only
    what a human sees. Variation selectors are kept unless they form a run of
    MIN_VS_RUN or more, so ordinary emoji survive."""
    drop = set()
    for s, e in _runs(text, VARIATION_SELECTORS):
        if e - s >= config.MIN_VS_RUN:
            drop.update(range(s, e))
    out = []
    for i, ch in enumerate(text):
        cp = ord(ch)
        if (
            i in drop
            or cp in TAG_RANGE
            or cp in BIDI_INITIATORS
            or cp in BIDI_POPS
            or cp in DEPRECATED_FORMAT
            or cp in ZERO_WIDTH
        ):
            continue
        out.append(ch)
    return "".join(out)


def has_hidden_unicode(text: str) -> bool:
    """True if ``text`` contains any code point the scanners look for."""
    return any(
        (cp := ord(ch)) in TAG_RANGE
        or cp in BIDI_INITIATORS
        or cp in BIDI_POPS
        or cp in DEPRECATED_FORMAT
        or cp in ZERO_WIDTH
        or cp in VARIATION_SELECTORS
        for ch in text
    )
