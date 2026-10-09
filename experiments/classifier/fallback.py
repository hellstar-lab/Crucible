"""NOT IN THE SHIPPING PATH. Negative result — see docs/classifier-experiment.md.

Offline fallback labelling: the original keyword heuristic.

Used only when the classifier model is unreachable (no key, bad key, network
down after retries) or when the caller asks for an offline scan. It is NOT
the primary path. It can recognise imperative language only, so anything it
does not match is left UNLABELLED (classification_failed), never called
benign: a fallback must not produce a cleaner result than the model would.
"""

from __future__ import annotations

import re

from . import config

_ATTACK_RE = re.compile("|".join(r"\b" + re.escape(k) for k in config.ATTACK_KEYWORDS), re.IGNORECASE)


def fallback_classify(text: str) -> str | None:
    """'ai_instruction' if the text matches the keyword heuristic, else None."""
    return "ai_instruction" if _ATTACK_RE.search(text) else None
