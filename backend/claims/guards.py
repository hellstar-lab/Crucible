"""Deterministic output guards that run after every stage (no model).

  rubric_leak            the output carries a distinctive phrase (>= LEAK_NGRAM content words in a row) of a
                         registered rubric example that the source does not contain: copied from the prompt, not
                         the document. Measured twice: "Engineered a Kafka-based event bus handling 5k events/s" for
                         an unrelated role line (also caught by the entity check), and "the command-line tool for
                         log search" as a guessed Stage 3 referent (caught by nothing else: no number, no entity).
  ungrounded_qualifier   a closed-lexicon intensifier ("proficient", "expert", "senior", ...) in the output that
                         the source unit does not contain. Measured: "Proficient in Python" from a plain skills list.
                         In a hiring context "Python" and "Proficient in Python" are different claims.

Both compare stemmed content words, so "handling" matches "handles" and "proficiency" matches "proficient".
"""

from __future__ import annotations

import re

from . import config
from .support import STOPWORDS, stem

_TOKEN = re.compile(r"[a-z0-9]+")


def content_words(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall((text or "").casefold()) if t not in STOPWORDS]


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def shared_ngram(a: str, b: str, n: int = config.LEAK_NGRAM) -> str | None:
    """A content-word n-gram present in both texts, or None."""
    common = _ngrams(content_words(a), n) & _ngrams(content_words(b), n)
    return " ".join(sorted(common)[0]) if common else None


def rubric_leak(output: str, source: str, examples: dict[str, list[str]] | None = None) -> tuple[str, str] | None:
    """(example, shared n-gram) when the output copies a registered example phrase the source lacks."""
    out_grams = _ngrams(content_words(output), config.LEAK_NGRAM)
    src_grams = _ngrams(content_words(source), config.LEAK_NGRAM)
    for stage_examples in (examples or config.RUBRIC_EXAMPLES).values():
        for ex in stage_examples:
            hit = (_ngrams(content_words(ex), config.LEAK_NGRAM) & out_grams) - src_grams
            if hit:
                return ex, " ".join(sorted(hit)[0])
    return None


# One canonical form per word family (the closed lexicon is small enough to list; a light stemmer does not join
# "expert" and "expertise").
_FAMILY = {"proficiency": "proficient", "expertise": "expert", "extensively": "extensive", "deeply": "deep",
           "strongly": "strong", "fluency": "fluent"}


def _canon(word: str) -> str | None:
    w = _FAMILY.get(word, word)
    return w if w in config.UNGROUNDED_QUALIFIERS else None


def ungrounded_qualifiers(output: str, source: str) -> list[str]:
    """Closed-lexicon qualifiers in the output (as written) whose word family is absent from the source."""
    src = {_canon(w) for w in _TOKEN.findall((source or "").casefold())} - {None}
    out = []
    for word in _TOKEN.findall((output or "").casefold()):
        c = _canon(word)
        if c and c not in src and word not in out:
            out.append(word)
    return out
