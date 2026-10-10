"""Extract declarative sentences from a README. Extraction only: nothing is
interpreted or scored here (Phase 4 does that with models)."""

from __future__ import annotations

import re

from backend.contracts import ReadmeClaim

MAX_CLAIMS = 60
README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README", "readme.md", "Readme.md")

_FENCE = re.compile(r"^\s*(```|~~~)")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!])\s+(?=[A-Z0-9])")
_INLINE_MD = re.compile(r"[*_`]{1,3}")


def _clean(text: str) -> str:
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    text = _INLINE_MD.sub("", text)
    return " ".join(text.split())


def _declarative(text: str) -> bool:
    return len(text.split()) >= 4 and not text.endswith(("?", ":")) and any(c.isalpha() for c in text)


def extract_claims(readme: str) -> list[ReadmeClaim]:
    claims: list[ReadmeClaim] = []
    in_fence = False
    paragraph: list[tuple[int, str]] = []

    def flush():
        if not paragraph:
            return
        start = paragraph[0][0]
        for sentence in _SENTENCE_SPLIT.split(_clean(" ".join(t for _n, t in paragraph))):
            sentence = sentence.strip()
            if _declarative(sentence):
                claims.append(ReadmeClaim(text=sentence[:400], kind="sentence", line=start))
        paragraph.clear()

    for number, raw in enumerate(readme.splitlines(), start=1):
        if _FENCE.match(raw):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "|", ">", "<!--", "===", "---")) or stripped.startswith("    "):
            flush()
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            flush()
            text = _clean(bullet.group(1))
            if _declarative(text):
                claims.append(ReadmeClaim(text=text[:400], kind="bullet", line=number))
            continue
        paragraph.append((number, stripped))
    flush()
    return claims[:MAX_CLAIMS]
