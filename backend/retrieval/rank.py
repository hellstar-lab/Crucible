"""Ranking and the 10-item cap. No embeddings, no model, no network.

Order: exact-tier match > fuzzy match, then authored_by_candidate True > None > False, then BM25 score of the item
(path tokens, excerpt, technology) against the claim's words (bm25s, MIT), then path for a stable tie-break.
``score`` on each item is its BM25 score (0.0 when only one candidate, where there is nothing to rank).
"""

from __future__ import annotations

import re

from backend.contracts import EvidenceItem

from . import config

_TOKEN = re.compile(r"[a-z0-9]+")
_FUZZY = {"fuzzy"}


def _toks(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower().replace("_", " ").replace("/", " ").replace(".", " ")) if len(t) > 1]


def bm25_scores(claim: str, items: list[EvidenceItem]) -> list[float]:
    if len(items) < 2:
        return [0.0] * len(items)
    import bm25s

    corpus = [_toks(" ".join(x for x in (it.path, it.excerpt, it.technology, it.match_detail) if x)) or ["_"] for it in items]
    query = _toks(claim)
    if not query:
        return [0.0] * len(items)
    retriever = bm25s.BM25()
    retriever.index(corpus, show_progress=False)
    return [round(float(s), 6) for s in retriever.get_scores(query)]


def rank(claim: str, items: list[EvidenceItem]) -> tuple[list[EvidenceItem], int]:
    """(top MAX_ITEMS items, number dropped)."""
    scores = bm25_scores(claim, items)
    for it, s in zip(items, scores):
        it.score = s
    authored_rank = {True: 2, None: 1, False: 0}
    stable = sorted(items, key=lambda it: (it.repo_full_name, it.path or "", it.start_line or 0, it.rule))  # deterministic ties
    ordered = sorted(stable, key=lambda it: (0 if it.match in _FUZZY else 1, authored_rank[it.authored_by_candidate], it.score),
                     reverse=True)
    return ordered[: config.MAX_ITEMS], max(0, len(ordered) - config.MAX_ITEMS)
