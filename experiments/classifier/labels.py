"""NOT IN THE SHIPPING PATH. Negative result — see docs/classifier-experiment.md.

How the experiment applied labels to a scan: one classifier call per hidden
fragment, then a three-way severity status. Kept only so the experiment can
be reproduced; the shipping status is two-state (backend.shield.status_for).
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from backend.contracts import HiddenFragment

from . import config
from .classify import ClassificationResult, classify_fragment, failed_result, fallback_result, require_opt_in

log = logging.getLogger(__name__)

Severity = Literal["clean", "suspicious", "attack_detected"]
_ATTACK_LABELS = {"ai_instruction", "fabricated_qualification"}


def severity_status(fragments: list[HiddenFragment]) -> Severity:
    """The experiment's three-way rule: any ai_instruction or
    fabricated_qualification -> attack_detected; any other finding,
    including classification_failed -> suspicious; none -> clean."""
    if not fragments:
        return "clean"
    if any(f.classification in _ATTACK_LABELS for f in fragments):
        return "attack_detected"
    return "suspicious"


def _apply(fragment: HiddenFragment, result: ClassificationResult) -> None:
    fragment.classification = result.label
    fragment.classification_confidence = result.confidence
    fragment.classification_failed = result.classification_failed


def label_fragments(fragments: list[HiddenFragment], *, experiment: bool = False, use_model: bool = True) -> bool:
    """Label fragments in place, one model call each. Requires
    experiment=True. Returns True if degraded to the keyword fallback."""
    require_opt_in(experiment)
    degraded = not use_model or not os.environ.get("NEBIUS_API_KEY")
    for i, frag in enumerate(fragments):
        if degraded:
            result = fallback_result(frag.text)
        elif i >= config.MAX_CLASSIFIED_FRAGMENTS:
            result = failed_result()
        else:
            result = classify_fragment(frag.text, experiment=True)
            if result.degraded:
                degraded = True
        _apply(frag, result)
    return degraded
