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
  4. otherwise ONE Super request with n=3 samples at temperature 0.7 (F2). Each sample gets the citation check (5) and
     the matrix (6) on its own. Super unanimous -> that verdict. Super NOT unanimous -> ONE Ultra request, n=3 (H0b);
     Ultra unanimous -> Ultra's verdict; Ultra not unanimous either -> inconclusive, "verdicts disagreed", with both
     sample sets recorded. Not a majority vote: 2-of-3 is exactly the instability the ensemble exists to expose. Ultra
     is also asked after a unanimous Super verdict that is contradicted or whose lowest chosen-label probability is
     below ESCALATION_MIN_TOP_PROB. Measured (G5): Super unanimity alone turned 3 of 6 ownership runs inconclusive.
     Every escalation and its reason is recorded.
  5. supported / contradicted without a citation that resolves to an item -> inconclusive (counted).
  6. the verdict matrix with the CITED item's authorship fields and the claim's SCOPE (F1: a contradiction needs the
     evidence and the claim to be about the same object).
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


def _scope(claim) -> tuple[str, str | None]:
    """The claim's scope from Stage 5; computed with the same deterministic rule when an older claim lacks it."""
    from backend.claims.routing import scope_of

    scope = _get(claim, "scope")
    if scope:
        return scope, _get(claim, "scope_target")
    return scope_of(_get(claim, "text") or "", _get(claim, "kind") or "")


def _judge_sample(sample: dict, es, kind, scope, target, counters: Counter) -> dict:
    """Citation check + matrix for ONE sampled judgement."""
    data = sample["data"]
    label = data.get("content_supported")
    idx = data.get("cited_item_index")
    cited = es.items[idx] if isinstance(idx, int) and 0 <= idx < len(es.items) else None
    if sample.get("error"):
        out = matrix.Outcome("inconclusive", f"sample error: {sample['error']}", "sample_error")
    elif label in ("supported", "contradicted") and cited is None:
        counters["citation_downgraded"] += 1
        out = matrix.Outcome("inconclusive", f"the model answered {label} without a citation that resolves to an evidence item "
                             f"(cited_item_index={idx!r}, {len(es.items)} items)", "citation_downgraded", ["gate:citation_unresolved"])
    elif label not in ("supported", "contradicted", "no_evidence", "inconclusive"):
        counters["unparseable_label"] += 1
        out = matrix.Outcome("inconclusive", f"no valid content label in the reply ({label!r})", "unparseable_label")
    else:
        out = matrix.decide(label, kind, cited, scope, target)
    top = ((sample.get("distribution") or {}).get("values", {}).get(label) or {}).get("prob") if label else None
    if sample.get("greedy_disagreement"):
        counters["greedy_disagreement"] += 1
    counters["samples"] += 1
    return {"label": label, "cited_item_index": idx, "cited": cited, "outcome": out, "top_prob": top,
            "evidence_summary": data.get("evidence_summary"), "content_reason": data.get("content_reason"),
            "greedy_disagreement": sample.get("greedy_disagreement", False), "error": sample.get("error")}


def _round(claim_text, es, tier, kind, scope, target, counters, run_tag):
    j = judge.judge(claim_text, es, tier, run_tag=run_tag)
    judged = [_judge_sample(s, es, kind, scope, target, counters) for s in j["samples"]]
    finals = [x["outcome"].verdict for x in judged]
    unanimous = len(judged) == config.ENSEMBLE_N and len(set(finals)) == 1
    return j, judged, unanimous


def _record(judged, tier) -> list[dict]:
    return [{"tier": tier, "content_supported": x["label"], "cited_item_index": x["cited_item_index"],
             "final_verdict": x["outcome"].verdict, "matrix_row": x["outcome"].row, "content_reason": x["content_reason"],
             "top_prob": x["top_prob"], "greedy_disagreement": x["greedy_disagreement"], "error": x["error"]} for x in judged]


def verdict_for(claim, es, counters: Counter | None = None, run_tag: str | None = None) -> tuple[ClaimVerdict, int, float | None]:
    """(verdict, model requests made, cost). Pure code for gates 1-3; otherwise one Super ensemble request (n=3) and at
    most one Ultra ensemble request (on disagreement, contradiction or low probability)."""
    counters = counters if counters is not None else Counter()
    base = _base(claim, es)
    scope, target = _scope(claim)
    base.update(scope=scope, scope_target=target)
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

    kind = base["claim_kind"]
    j, judged, unanimous = _round(base["claim_text"], es, config.CONTENT_TIER, kind, scope, target, counters, run_tag)
    calls, cost = 1, j["cost_usd"] or 0.0
    samples = _record(judged, j["tier"])
    escalated, why = False, None
    if not unanimous:  # H0b: disagreement escalates; it is not an abstention and not a majority vote
        why = "Super samples disagreed (" + ", ".join(x["outcome"].verdict for x in judged) + ")"
    else:
        final = judged[0]["outcome"].verdict
        low = min((x["top_prob"] for x in judged if x["top_prob"] is not None), default=None)
        if final == "contradicted":
            why = "unanimous verdict contradicted"
        elif low is not None and low < config.ESCALATION_MIN_TOP_PROB:
            why = f"lowest sample probability of the chosen label {low:.3f} < {config.ESCALATION_MIN_TOP_PROB}"
    if why:
        j, judged, unanimous = _round(base["claim_text"], es, config.ESCALATION_TIER, kind, scope, target, counters, run_tag)
        calls += 1
        cost += j["cost_usd"] or 0.0
        samples += _record(judged, j["tier"])
        escalated = True
        counters["escalations"] += 1
        counters["escalations_on_disagreement" if why.startswith("Super samples disagreed") else "escalations_on_check"] += 1
        if unanimous and why.startswith("Super samples disagreed"):
            counters["disagreements_resolved_by_ultra"] += 1
    counters["judgements"] += 1
    agreement = f"{max((sum(1 for x in judged if x['outcome'].verdict == v) for v in {x['outcome'].verdict for x in judged}), default=0)}/{len(judged)}"
    common = dict(model_tier=j["tier"], escalated=escalated, escalation_reason=why, samples=samples, agreement=agreement,
                  prompt_hash=j["prompt_hash"], cost_usd=round(cost, 8),
                  greedy_disagreement=any(x["greedy_disagreement"] for x in judged))
    if not unanimous:
        counters["ensemble_disagreements"] += 1
        tiers = "Super and Ultra" if escalated else "the samples"
        return ClaimVerdict(**base, verdict="inconclusive", reason=f"verdicts disagreed ({tiers}; every sample is recorded)",
                            matrix_row="ensemble:disagreed",
                            gates=["gate:ensemble_disagreed"], **common), calls, cost
    x = judged[0]
    out = x["outcome"]
    return ClaimVerdict(**base, verdict=out.verdict, reason=out.reason, matrix_row=out.row, gates=out.gates,
                        authorship_note=out.authorship_note, scope_match_rule=out.scope_match_rule,
                        scope_match_score=out.scope_match_score, evidence_summary=x["evidence_summary"], content_reason=x["content_reason"],
                        content_supported=x["label"] if x["label"] in ("supported", "contradicted", "no_evidence", "inconclusive") else None,
                        cited_item_index=x["cited_item_index"], cited_item=x["cited"], citation_downgraded=out.row == "citation_downgraded",
                        value_distribution=None, **common), calls, cost


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
    n_samples = counters["samples"]
    return ClaimVerdicts(verdicts=out, not_judged=not_judged, truncated=truncated, calls=calls, cost_usd=round(cost, 8),
                         cost_cap_usd=cap, counters=dict(counters),
                         greedy_disagreement_rate=round(counters["greedy_disagreement"] / n_samples, 4) if n_samples else None)
