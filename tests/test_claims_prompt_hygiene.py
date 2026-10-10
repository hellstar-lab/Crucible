"""T3/T5: rubric examples are registered data, and no prompt contains a string from the eval set. Offline."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.claims import config, guards, stages

EVAL = Path(__file__).resolve().parent / "eval" / "extraction_set.jsonl"
RUBRICS = {"selection": stages.SELECTION_RUBRIC, "disambiguation": stages.DISAMBIGUATION_RUBRIC,
           "decomposition": stages.DECOMPOSITION_RUBRIC}
PROMPT_TEXT = "\n".join([stages.PREAMBLE, *RUBRICS.values()])


def _eval_strings() -> list[tuple[str, str]]:
    """(row id, string) for every string in the eval set: inputs, context, expectations."""
    out = []
    for line in EVAL.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)

        def walk(x):
            if isinstance(x, str):
                out.append((row["id"], x))
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk({k: v for k, v in row.items() if k not in ("id", "notes")})
    return out


def _significant(s: str) -> bool:
    """A quotable string: at least 3 content words, and not a label identifier such as no_verifiable_claims
    (label names are the shared vocabulary of prompt and eval, not contamination)."""
    return not re.fullmatch(r"[a-z_]+", s) and len(guards.content_words(s)) >= 3


def _norm(s: str) -> str:
    return " ".join(re.sub(r"^[\s•◦\-*]+", "", s).casefold().split())


def test_every_quoted_rubric_string_is_registered():
    for stage, rubric in RUBRICS.items():
        quoted = [" ".join(q.split()) for q in re.findall(r'"([^"]+)"', rubric)]  # a quote may wrap across lines
        missing = [q for q in quoted if q not in config.RUBRIC_EXAMPLES[stage]]
        assert not missing, f"{stage} rubric quotes unregistered examples: {missing}"
        flat = " ".join(rubric.split())
        absent = [e for e in config.RUBRIC_EXAMPLES[stage] if e not in flat]
        assert not absent, f"{stage}: registered examples not in the rubric: {absent}"


def overlaps() -> list[tuple[str, str, str]]:
    """(row id, eval string, why) for every eval string a prompt contains, verbatim or as a distinctive n-gram."""
    hits = []
    prompt_norm = _norm(PROMPT_TEXT)
    for rid, s in _eval_strings():
        if not _significant(s):
            continue
        if _norm(s) in prompt_norm:
            hits.append((rid, s, "verbatim in a prompt"))
            continue
        shared = guards.shared_ngram(PROMPT_TEXT, s, config.LEAK_NGRAM)
        if shared:
            hits.append((rid, s, f"shares the {config.LEAK_NGRAM}-gram {shared!r} with a prompt"))
    for stage, examples in config.RUBRIC_EXAMPLES.items():
        for e in examples:
            if _significant(e):
                for rid, s in _eval_strings():
                    if _norm(e) in _norm(s) and (rid, s, "verbatim in a prompt") not in hits:
                        hits.append((rid, s, f"contains the {stage} example {e!r}"))
    return hits


def test_no_prompt_contains_an_eval_string():
    assert overlaps() == []


def test_the_overlap_test_fails_when_an_overlapping_example_is_added(monkeypatch):
    poisoned = stages.SELECTION_RUBRIC + '\n(for example "Passionate about clean architecture and elegant code")'
    monkeypatch.setattr(stages, "SELECTION_RUBRIC", poisoned)
    monkeypatch.setitem(RUBRICS, "selection", poisoned)
    monkeypatch.setattr(__import__(__name__), "PROMPT_TEXT", "\n".join([stages.PREAMBLE, *RUBRICS.values()]))
    assert any(rid == "w2_opinion" for rid, _s, _w in overlaps())
