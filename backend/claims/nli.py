"""Local NLI (P2-5, P2-6): one model for the entailment backstop and for semantic-entropy clustering.

Model: cross-encoder/nli-deberta-v3-base (Apache-2.0), pinned to a revision; runs locally on CPU, never calls an API.
Chosen over MiniCheck Flan-T5-Large: a three-way NLI model (entailment / neutral / contradiction) is what
bidirectional-entailment clustering needs (Farquhar et al., Nature 2024 use DeBERTa MNLI), it is 184M parameters
instead of 770M (~0.7 GB on disk instead of ~3 GB; the development machine had under 5 GB free), and it installs from
PyPI via transformers. (The PyPI name "minicheck" is an unrelated package.)

Sentence-level: a multi-sentence hypothesis is split and EVERY sentence must be entailed.

The model loads lazily and only from the local cache unless ``allow_download`` is set; nothing here touches the network
at import time, and tests use the cache or skip.
"""

from __future__ import annotations

import re
from functools import lru_cache

MODEL_ID = "cross-encoder/nli-deberta-v3-base"
REVISION = "6c749ce3425cd33b46d187e45b92bbf96ee12ec7"
LABELS = ("contradiction", "entailment", "neutral")  # id2label of this model
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


class NLIUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _model(allow_download: bool = False):
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise NLIUnavailable("torch/transformers are not installed") from exc
    try:
        tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=REVISION, local_files_only=not allow_download)
        mod = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, revision=REVISION, local_files_only=not allow_download)
    except OSError as exc:
        raise NLIUnavailable(f"{MODEL_ID}@{REVISION[:8]} is not in the local cache") from exc
    mod.eval()
    assert tuple(mod.config.id2label[i] for i in range(3)) == LABELS
    return tok, mod


def available() -> bool:
    try:
        _model()
        return True
    except NLIUnavailable:
        return False


def scores(premise: str, hypothesis: str) -> dict[str, float]:
    """{contradiction, entailment, neutral} probabilities for one premise / hypothesis pair."""
    import torch

    tok, mod = _model()
    with torch.no_grad():
        enc = tok(premise, hypothesis, return_tensors="pt", truncation=True, max_length=512)
        probs = torch.softmax(mod(**enc).logits[0], dim=-1).tolist()
    return {LABELS[i]: round(p, 6) for i, p in enumerate(probs)}


def label(premise: str, hypothesis: str) -> str:
    s = scores(premise, hypothesis)
    return max(s, key=s.get)


def sentences(text: str) -> list[str]:
    return [s for s in (x.strip() for x in _SENT.split(text or "")) if s]


def entails(premise: str, hypothesis: str) -> tuple[bool, list[dict]]:
    """(every sentence of the hypothesis is entailed, per-sentence scores)."""
    per = [{"sentence": s, **scores(premise, s)} for s in sentences(hypothesis)]
    ok = bool(per) and all(max(("contradiction", "entailment", "neutral"), key=lambda k: x[k]) == "entailment" for x in per)
    return ok, per


def equivalent(a: str, b: str) -> bool:
    """Bidirectional entailment: a entails b AND b entails a (the semantic-entropy clustering relation)."""
    return entails(a, b)[0] and entails(b, a)[0]
