"""Phase 4C: verdicts. A 4A claim + its 4B EvidenceSet -> a ClaimVerdict (one row of the evidence table).

    verdict_for(claim, evidence_set)            -> ClaimVerdict
    verdicts_for(claims, evidence_sets)         -> ClaimVerdicts (per-document cost cap, truncated when it bites)

The model judges SEMANTICS only ("does this evidence support the content of this claim?"); code decides AUTHORSHIP
from 4B's blame data (matrix.py). The difference between "this code exists in a repo they pushed" and "they wrote
this code" IS the product, and a model that could get it wrong could invalidate the whole project.

Order of decisions, all in code:
  1. retrieval_performed False -> unverifiable, 4B's skip_reason verbatim. No model call.
  2. every technology the claim names leaves no code artifact -> inconclusive. No model call. Not no_evidence.
  3. no evidence items but a populated search -> no_evidence. No model call: there is nothing for a model to read, and
     4B's invariant already says this is "we looked and found nothing".
  4. otherwise one content judgement on Super. Escalated once to Ultra when the re-ranked probability of the chosen
     label is below ESCALATION_MIN_TOP_PROB, or the label is "contradicted". Every escalation is recorded.
  5. supported / contradicted without a citation that resolves to an item -> inconclusive (counted).
  6. the verdict matrix with the CITED item's authorship fields.
No scores, no ranking, no hire/reject signal anywhere.
"""

from __future__ import annotations

from collections import Counter

from backend import llm
from backend.contracts import ClaimVerdict, ClaimVerdicts
from backend.retrieval import lexicon

from . import config, judge, matrix


def _get(claim, k):
    return claim.get(k) if isinstance(claim, dict) else getattr(claim, k, None)


def no_artifact_possible(claim, es) -> bool:
    """True when the claim names technologies and none of them can leave an artifact in source code."""
    techs = (es.searched.technologies if es.searched else None) or lexicon.recognise(_get(claim, "text") or "")
    lex = lexicon.technologies()
    return bool(techs) and not es.items and all(not lex[t].get("code_artifacts", True) for t in techs if t in lex)


def _base(claim, es) -> dict:
    return {"claim_id": _get(claim, "claim_id"), "claim_text": _get(claim, "text") or "", "claim_kind": _get(claim, "kind"),
            "retrieval_version": es.retrieval_version}


def verdict_for(claim, es, counters: Counter | None = None, run_tag: str | None = None) -> tuple[ClaimVerdict, int, float | None]:
    """(verdict, model calls made, cost). Pure code for gates 1-3; at most two calls otherwise (Super, then Ultra)."""
    counters = counters if counters is not None else Counter()
    base = _base(claim, es)
    if not es.retrieval_performed:
        counters["unverifiable"] += 1
        return ClaimVerdict(**base, verdict="unverifiable", reason=es.skip_reason or "retrieval was not performed",
                            matrix_row="gate:retrieval_not_performed", gates=["gate:retrieval_not_performed"]), 0, 0.0
    if no_artifact_possible(claim, es):
        counters["no_artifact_possible"] += 1
        return ClaimVerdict(**base, verdict="inconclusive", reason=matrix.NO_ARTIFACT_REASON, matrix_row="gate:no_artifact_possible",
                            gates=["gate:no_artifact_possible"]), 0, 0.0
    if not es.items:
        counters["no_evidence_without_call"] += 1
        excluded = [m for m in (es.searched.rules_fired if es.searched else []) if "excluded_by_3.5" in m]
        reason = "the search covered the listed repositories and found nothing bearing on the claim"
        if excluded:
            reason += f" (note: {', '.join(excluded)}: an entry exists but the 3.5 exclusion rules removed it)"
        return ClaimVerdict(**base, verdict="no_evidence", reason=reason, matrix_row="no_evidence:empty_items"), 0, 0.0

    calls, cost = 1, 0.0
    j = judge.judge(base["claim_text"], es, config.CONTENT_TIER, run_tag=run_tag)
    cost += j["cost_usd"] or 0.0
    label = j["data"].get("content_supported")
    top = (j["distribution"] or {}).get("values", {}).get(label, {}).get("prob") if label else None
    escalated, why = False, None
    if label == "contradicted" or (top is not None and top < config.ESCALATION_MIN_TOP_PROB):
        why = "content label contradicted" if label == "contradicted" else f"top label probability {top:.3f} < {config.ESCALATION_MIN_TOP_PROB}"
        j = judge.judge(base["claim_text"], es, config.ESCALATION_TIER, run_tag=run_tag)
        calls += 1
        cost += j["cost_usd"] or 0.0
        escalated = True
        counters["escalations"] += 1
        label = j["data"].get("content_supported")
    if j["greedy_disagreement"]:
        counters["greedy_disagreement"] += 1
    counters["judgements"] += 1

    idx = j["data"].get("cited_item_index")
    cited = es.items[idx] if isinstance(idx, int) and 0 <= idx < len(es.items) else None
    downgraded = False
    if label in ("supported", "contradicted") and cited is None:
        counters["citation_downgraded"] += 1
        downgraded = True
        out = matrix.Outcome("inconclusive", f"the model answered {label} without a citation that resolves to an evidence item "
                             f"(cited_item_index={idx!r}, {len(es.items)} items)", "citation_downgraded", ["gate:citation_unresolved"])
    elif label not in ("supported", "contradicted", "no_evidence", "inconclusive"):
        counters["unparseable_label"] += 1
        out = matrix.Outcome("inconclusive", f"no valid content label in the reply ({label!r})", "unparseable_label")
    else:
        out = matrix.decide(label, base["claim_kind"], cited)
    return ClaimVerdict(**base, verdict=out.verdict, reason=out.reason, matrix_row=out.row, gates=out.gates,
                        authorship_note=out.authorship_note, evidence_summary=j["data"].get("evidence_summary"),
                        content_reason=j["data"].get("content_reason"), content_supported=label if label in
                        ("supported", "contradicted", "no_evidence", "inconclusive") else None, cited_item_index=idx,
                        cited_item=cited, citation_downgraded=downgraded, model_tier=j["tier"], escalated=escalated,
                        escalation_reason=why, value_distribution=j["distribution"], greedy_disagreement=j["greedy_disagreement"],
                        prompt_hash=j["prompt_hash"], cost_usd=round(cost, 8)), calls, cost


def verdicts_for(claims, evidence_sets, cost_cap_usd: float | None = None, run_tag: str | None = None) -> ClaimVerdicts:
    cap = config.DEFAULT_COST_CAP_USD if cost_cap_usd is None else cost_cap_usd
    by_id = {es.claim_id: es for es in evidence_sets}
    counters: Counter = Counter()
    out, not_judged, calls, cost, truncated = [], [], 0, 0.0, False
    for claim in claims:
        cid = _get(claim, "claim_id")
        es = by_id.get(cid)
        if es is None:
            not_judged.append(cid)
            counters["no_evidence_set"] += 1
            continue
        # projected spend: stop BEFORE a call that would take the document over the cap (not after it)
        if es.retrieval_performed and es.items and cost + config.SUPER_REFERENCE_CALL_USD > cap:
            truncated = True
            not_judged.append(cid)
            continue
        try:
            v, n, c = verdict_for(claim, es, counters, run_tag=run_tag)
        except llm.LLMRefusalError as exc:
            counters["model_refused"] += 1
            v = ClaimVerdict(**_base(claim, es), verdict="inconclusive", reason=f"the model refused: {exc.refusal}",
                             matrix_row="model_refused")
            n, c = 1, 0.0
        except llm.LLMJSONError:
            counters["unparseable_reply"] += 1
            v = ClaimVerdict(**_base(claim, es), verdict="inconclusive", reason="the model reply was not JSON", matrix_row="unparseable_reply")
            n, c = 1, 0.0
        out.append(v)
        calls += n
        cost += c or 0.0
    judged = counters["judgements"]
    return ClaimVerdicts(verdicts=out, not_judged=not_judged, truncated=truncated, calls=calls, cost_usd=round(cost, 8),
                         cost_cap_usd=cap, counters=dict(counters),
                         greedy_disagreement_rate=round(counters["greedy_disagreement"] / judged, 4) if judged else None)
