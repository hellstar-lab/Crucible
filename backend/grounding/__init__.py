"""Phase 4D: grounding check. The last gate before a verdict reaches a human. Deterministic, zero model calls.

Why: asked to fetch example.com, all three model tiers invented a plausible fetch timestamp instead of saying they
could not browse. A model will fabricate a citation that reads correctly, so every citation behind a supported or
contradicted content label is re-checked against the data, never against the model's words:

  c_index     the cited index resolves to an item of THIS claim's EvidenceSet
  c_path      that item's path exists in the analysed tree (the 3.6 snapshot at head_sha)
  c_lines     its start_line..end_line exists in that file (1 <= start <= end <= length); an item with no line range
              records not_applicable, never pass
  c_authored  for a supported verdict on an ownership claim: the candidate authored EVERY source line of the cited
              range per blame (the whole file when the item has no range), not merely something in the file

A check that cannot be performed (no snapshot, no identity, length unknown) records ``unavailable`` and counts as a
failure: a verdict that cannot be grounded is not grounded. Any failure downgrades the verdict to inconclusive with the
failed check named verbatim and increments grounding_failures[check]. A downgrade is never silent and never a crash.
A citation is never repaired: a wrong citation is a downgrade, not something to fix.
"""

from __future__ import annotations

from collections import Counter

from backend.contracts import ClaimVerdict, EvidenceSet

CHECKS = ("c_index", "c_path", "c_lines", "c_authored")


def _view_for(views, repo_full_name):
    return next((v for v in views if v.full_name == repo_full_name), None)


def check(verdict: ClaimVerdict, es: EvidenceSet, views) -> tuple[dict[str, str], dict[str, str]]:
    """(results, failure details) for one verdict. results[check] in pass | fail | not_applicable | unavailable."""
    res, why = {}, {}
    idx = verdict.cited_item_index
    if not (isinstance(idx, int) and 0 <= idx < len(es.items)):
        res["c_index"], why["c_index"] = "fail", f"cited_item_index {idx!r} does not resolve ({len(es.items)} evidence items)"
        for c in CHECKS[1:]:
            res[c] = "not_applicable"
        return res, why
    res["c_index"] = "pass"
    item = es.items[idx]
    view = _view_for(views, item.repo_full_name)

    if item.path is None:
        res["c_path"] = "not_applicable"
    else:
        present = view.in_snapshot(item.path) if view else None
        if present is None:
            res["c_path"], why["c_path"] = "unavailable", f"no snapshot of {item.repo_full_name} to confirm {item.path!r} exists"
        elif present:
            res["c_path"] = "pass"
        else:
            res["c_path"], why["c_path"] = "fail", f"{item.path!r} is not in {item.repo_full_name} at {view.head_sha}"

    if item.start_line is None and item.end_line is None:
        res["c_lines"] = "not_applicable"
    elif res["c_path"] != "pass":
        res["c_lines"], why["c_lines"] = "unavailable", "the file could not be confirmed, so its lines cannot be"
    else:
        start, end = item.start_line, item.end_line if item.end_line is not None else item.start_line
        n = view.line_count(item.path)
        if n is None:
            res["c_lines"], why["c_lines"] = "unavailable", f"length of {item.path!r} unknown (not counted, no checkout)"
        elif start is None or start < 1 or end < start or end > n:
            res["c_lines"], why["c_lines"] = "fail", f"lines {start}..{end} do not exist in {item.path!r} ({n} lines)"
        else:
            res["c_lines"] = "pass"

    if verdict.verdict == "supported" and verdict.claim_kind in ("ownership", "technology_use"):
        # ownership: EVERY source line of the cited span is the candidate's; technology_use (K1): AT LEAST ONE is, i.e.
        # the technology appears in code the candidate wrote
        flags = view.line_flags(item.path) if (view and item.path) else None
        if flags is None:
            res["c_authored"], why["c_authored"] = "unavailable", "no line-level blame for the cited file under the candidate identity"
        else:
            start = item.start_line or 1
            end = item.end_line or item.start_line or len(flags)
            span = flags[start - 1:end]
            source = [a for s, a in span if s] or [a for _s, a in span]
            need = any if verdict.claim_kind == "technology_use" else all
            if span and need(source):
                res["c_authored"] = "pass"
            else:
                mine = sum(1 for a in source if a)
                res["c_authored"], why["c_authored"] = "fail", (f"the candidate authored {mine} of {len(source)} source lines in "
                                                               f"{item.path}:{start}-{end}, "
                                                               + ("none of them" if need is any else "not all of them"))
    else:
        res["c_authored"] = "not_applicable"
    return res, why


def ground(verdict: ClaimVerdict, es: EvidenceSet, views, failures: Counter | None = None) -> ClaimVerdict:
    """The verdict confirmed (grounding_passed True) or downgraded to inconclusive with the failed check named."""
    failures = failures if failures is not None else Counter()
    if verdict.content_supported not in ("supported", "contradicted"):
        return verdict  # nothing cited to ground: no_evidence / inconclusive / unverifiable / ensemble disagreement
    res, why = check(verdict, es, views)
    failed = [c for c in CHECKS if res[c] in ("fail", "unavailable")]
    if not failed:
        return verdict.model_copy(update={"grounding_checks": res, "grounding_passed": True})
    for c in failed:
        failures[c] += 1
    first = failed[0]
    reason = f"grounding check {first} failed: {why[first]}" + (f" (also failed: {', '.join(failed[1:])})" if failed[1:] else "")
    return verdict.model_copy(update={"grounding_checks": res, "grounding_passed": False, "pre_grounding_verdict": verdict.verdict,
                                      "verdict": "inconclusive", "reason": reason, "superseded_reason": verdict.reason,
                                      "matrix_row": f"grounding:{first}",
                                      "gates": verdict.gates + [f"grounding:{c}" for c in failed]})


def ground_all(verdicts, evidence_sets, views) -> tuple[list[ClaimVerdict], dict[str, int]]:
    by_id = {es.claim_id: es for es in evidence_sets}
    failures: Counter = Counter()
    out = [ground(v, by_id[v.claim_id], views, failures) if v.claim_id in by_id else v for v in verdicts]
    return out, {c: failures.get(c, 0) for c in CHECKS}
