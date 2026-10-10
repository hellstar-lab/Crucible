"""Stage 3 ensemble (P2-3) and semantic entropy (P2-6) over n sampled resolutions.

P2-3: one request, n=3, temperature 0.7 (measured: n <= 3, and n > 1 requires temperature > 0). UNANIMITY:
  3 of 3 agree on "resolved" with the same resolution -> resolved, with that resolution
  3 of 3 say cannot_be_disambiguated              -> cannot_be_disambiguated
  anything else                                    -> cannot_be_disambiguated, reason "resolutions disagreed", all three
                                                      samples recorded so the disagreement is auditable
Not majority: 2-of-3 is too weak for a referent no downstream check can verify. Over-refusing drops a claim; guessing
ships a fabricated claim about a real person.

"Same resolution" is strict on purpose: the two rewrites are equal after normalising case, spacing and punctuation, OR
they add the SAME words to the unit (the resolved referent) AND entail each other. NLI alone is too lenient for
referents: it judged "Improved the role by 40%" and "Improved the experience by 40%" equivalent.

P2-6: semantic entropy (Farquhar et al., Nature 2024). The samples are clustered by bidirectional entailment, using the
same local NLI model as the backstop (no second dependency). The entropy is the discrete form,
-sum p(c) log p(c), with p(c) = |c| / n. Refusals form their own cluster. With n=3 it takes one of three values:
0 (all agree), 0.637 (2+1) or 1.099 (all differ).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from .guards import content_words

RESOLVED, REFUSED = "resolved", "cannot_be_disambiguated"


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s%]", " ", (text or "").casefold()).split())


def added_words(unit_text: str, clarified: str | None) -> frozenset[str]:
    """Content-word stems the rewrite adds to the unit: the resolved referent."""
    return frozenset(set(content_words(clarified or "")) - set(content_words(unit_text)))


def same_resolution(a: str | None, b: str | None, unit_text: str, equivalent: Callable[[str, str], bool] | None) -> bool:
    if _norm(a) == _norm(b):
        return True
    if added_words(unit_text, a) != added_words(unit_text, b):
        return False
    return bool(equivalent and equivalent(a or "", b or ""))


def decide(samples: list[dict], unit_text: str, equivalent: Callable[[str, str], bool] | None = None) -> dict:
    """samples: [{verdict, clarified_text, failed_referent, reason, error}] -> {verdict, clarified_text, failed_referent,
    reason, unanimous}."""
    verdicts = [s.get("verdict") if not s.get("error") else None for s in samples]
    if samples and all(v == REFUSED for v in verdicts):
        return {"verdict": REFUSED, "clarified_text": None, "unanimous": True,
                "failed_referent": samples[0].get("failed_referent"), "reason": samples[0].get("reason")}
    if samples and all(v == RESOLVED for v in verdicts):
        first = samples[0].get("clarified_text")
        if all(same_resolution(first, s.get("clarified_text"), unit_text, equivalent) for s in samples[1:]):
            return {"verdict": RESOLVED, "clarified_text": first, "unanimous": True, "failed_referent": None,
                    "reason": samples[0].get("reason")}
    return {"verdict": REFUSED, "clarified_text": None, "unanimous": False, "failed_referent": None,
            "reason": "resolutions disagreed"}


def clusters(samples: list[dict], equivalent: Callable[[str, str], bool]) -> list[list[int]]:
    """Bidirectional-entailment clusters of sample indices. Refusals and errors form their own clusters."""
    out: list[list[int]] = []
    reps: list[tuple[str, str | None]] = []
    for i, s in enumerate(samples):
        kind = "error" if s.get("error") else s.get("verdict")
        text = s.get("clarified_text") if kind == RESOLVED else None
        for c, (rk, rt) in zip(out, reps):
            if rk == kind and (kind != RESOLVED or _norm(rt) == _norm(text) or equivalent(rt or "", text or "")):
                c.append(i)
                break
        else:
            out.append([i])
            reps.append((kind, text))
    return out


def semantic_entropy(cluster_list: list[list[int]]) -> float:
    n = sum(len(c) for c in cluster_list)
    return round(-sum((len(c) / n) * math.log(len(c) / n) for c in cluster_list), 4) if n else 0.0
