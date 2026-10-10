"""Phase 4D report: one row per claim plus a header. ASSEMBLY ONLY: every field is copied from the stage that produced
it (recorded per row in ``sources``); nothing is computed for the first time here. No scores, no ranking, no
hire/reject signal.

Authorship rendering rule (3.6, binding): an authored ratio is never shown without its confidence and, when it is not
meaningful, its reason. H0c: the ratio is shown as a percentage with at most one decimal (``fmt_ratio``), the
confidence reason is repeated on every claim row (copied from 3.6 for the cited item's repo), and languages with zero
lines are omitted.
"""

from __future__ import annotations

from backend.contracts import ClaimEvidenceReport, ReportHeader, ReportRow

from . import CHECKS

ROW_SOURCES = {"verdict": "4C matrix, after 4D grounding", "reason": "4C / 4D", "citation": "4B EvidenceItem (cited by 4C)",
               "authored_by_candidate": "4B (blame via the 3.6 snapshot)", "authored_ratio": "4B (3.5 per-file blame)",
               "authorship_caveat": "4B", "authorship_confidence": "3.6 Authorship", "authorship_note": "4C matrix",
               "authorship_confidence_reason": "3.6 Authorship", "agreement / model_tier / escalated": "4C ensemble",
               "matrix_row": "4C matrix", "scope": "4A Stage 5", "grounding": "4D"}

NOT_MEASURED = "not measured"
REPRODUCIBILITY_NOTE = ("Verdicts come from sampled model judgements: the platform is not deterministic even at temperature 0 "
                        "and ignores seed, so a re-run can change a verdict. Each judged row records the prompt hash of its "
                        "request and every sample is kept in the 4C verdict output. Retrieval, the verdict matrix and "
                        "grounding are deterministic code.")
PROVENANCE_COUNTERS = ("samples", "escalations", "escalations_on_disagreement", "disagreements_resolved_by_ultra",
                       "escalations_on_check", "ensemble_disagreements", "citation_downgraded")


def fmt_ratio(r) -> str:
    """A 0..1 ratio as a percentage with at most one decimal (0.6154 -> 61.5%, 1.0 -> 100%). Formatting only."""
    if r is None:
        return NOT_MEASURED
    t = f"{r * 100:.1f}"
    return (t[:-2] if t.endswith(".0") else t) + "%"


def _provenance(run, extraction, run_label) -> dict:
    """Run accounting copied from ClaimVerdicts / ClaimExtraction. A Counter key that is absent after a measured run
    means the event happened zero times, so it is recorded as 0; with no run at all every field is None."""
    c = run.counters if run is not None else None
    out = {"run_label": run_label,
           "model_requests": run.calls if run is not None else None,
           "cost_usd": run.cost_usd if run is not None else None,
           "cost_cap_usd": run.cost_cap_usd if run is not None else None,
           "truncated": run.truncated if run is not None else None,
           "not_judged": list(run.not_judged) if run is not None else None,
           "greedy_disagreement_rate": run.greedy_disagreement_rate if run is not None else None,
           **{k: (c.get(k, 0) if c is not None else None) for k in PROVENANCE_COUNTERS},
           "extraction_requests": getattr(extraction, "calls", None),
           "extraction_cost_usd": getattr(extraction, "cost_usd", None),
           "reproducibility_note": REPRODUCIBILITY_NOTE}
    return out


def build_report(*, verdicts, evidence_sets, target_reports, extraction=None, grounding_failures=None, run=None,
                 run_label: str | None = None) -> ClaimEvidenceReport:
    by_id = {es.claim_id: es for es in evidence_sets}
    auth_by_repo = {t.repo_facts.full_name: t.authorship for t in target_reports if t.repo_facts is not None}
    rows = []
    for v in verdicts:
        es = by_id.get(v.claim_id)
        idx = v.cited_item_index
        item = es.items[idx] if (es and isinstance(idx, int) and 0 <= idx < len(es.items) and v.content_supported in
                                 ("supported", "contradicted")) else None
        citation = None if item is None else {"repo": item.repo_full_name, "path": item.path, "start_line": item.start_line,
                                               "end_line": item.end_line}
        ra = auth_by_repo.get(item.repo_full_name) if item else None
        rows.append(ReportRow(
            claim_id=v.claim_id, claim_text=v.claim_text, claim_kind=v.claim_kind, verdict=v.verdict, reason=v.reason,
            citation=citation, authored_by_candidate=item.authored_by_candidate if item else None,
            authored_ratio=item.authored_ratio if item else None, authorship_caveat=item.authorship_caveat if item else None,
            authorship_confidence=item.authorship_confidence if item else None,
            authorship_confidence_reason=ra.authorship_confidence_reason if ra else None,
            likely_identity_gap=ra.likely_identity_gap if ra else None, authorship_note=v.authorship_note,
            content_reason=v.content_reason, matrix_row=v.matrix_row, agreement=v.agreement, model_tier=v.model_tier,
            escalated=v.escalated if v.samples else None, escalation_reason=v.escalation_reason, scope=v.scope,
            scope_target=v.scope_target, scope_match_rule=v.scope_match_rule, prompt_hash=v.prompt_hash,
            grounding_passed=v.grounding_passed, grounding_checks=v.grounding_checks,
            pre_grounding_verdict=v.pre_grounding_verdict, sources=ROW_SOURCES))

    analysed, excluded, authorship = [], [], []
    for t in target_reports:
        rf = t.repo_facts
        if rf is None:
            analysed.append({"input": t.raw_input, "status": t.status, "source": "3.6"})
            continue
        analysed.append({"repo": rf.full_name, "status": t.status, "ref": rf.ref_analyzed, "head_sha": rf.head_sha,
                         "scope": rf.scoped_path, "source_files": rf.source_files, "total_source_lines": rf.total_source_lines,
                         "languages": {k: n for k, n in (rf.languages or {}).items() if n} if rf.languages is not None else None,
                         "source": "3.6 repo_facts"})
        excluded.append({"repo": rf.full_name, "files_by_reason": {k: n for k, n in rf.excluded_files_by_reason.items() if n},
                         "lines_by_reason": {k: n for k, n in rf.excluded_lines_by_reason.items() if n},
                         "top_excluded": [f"{e.path} ({e.reason})" for e in rf.top_excluded_files[:5]], "source": "3.5 via 3.6"})
        a = t.authorship
        authorship.append({"repo": rf.full_name, "authored_ratio": a.authored_ratio if a else None,
                           "authorship_confidence": a.authorship_confidence if a else None,
                           "meaningful": a.authored_ratio_meaningful if a else None,
                           "reason": a.authorship_confidence_reason if a else "no identity: authorship was not computed",
                           "likely_identity_gap": a.likely_identity_gap if a else None, "source": "3.6 Authorship"})
    shield = None
    if extraction is not None:
        g = extraction.shield
        shield = {"document": extraction.document, "status": g.document_status, "gate_applied": g.applied, "detectors": g.detectors,
                  "hidden_fragments": g.hidden_fragments, "units_removed": g.units_removed, "needs_review": g.needs_review,
                  "source": "2C via the 4A Shield gate"}
    texts = {v.claim_id: v.claim_text for v in verdicts}
    not_checkable = [{"claim_id": es.claim_id, "claim_text": texts.get(es.claim_id), "kind": es.claim_kind,
                      "skip_reason": es.skip_reason, "source": "4B"} for es in evidence_sets if not es.retrieval_performed]
    excluded_units = [{"unit_id": u.unit_id, "section": u.source_section, "stage": u.stage, "reason_code": u.reason_code,
                       "detail": u.detail, "source": "4A"} for u in (getattr(extraction, "excluded", None) or [])]
    versions = {"retrieval": next((es.retrieval_version for es in evidence_sets), None)}
    header = ReportHeader(analysed=analysed, excluded=excluded, authorship=authorship, shield=shield, not_checkable=not_checkable,
                          grounding_failures=(None if grounding_failures is None else
                                              {c: grounding_failures.get(c, 0) for c in CHECKS}), excluded_units=excluded_units,
                          provenance=_provenance(run, extraction, run_label), stage_versions=versions)
    return ClaimEvidenceReport(header=header, rows=rows)


def render(report: ClaimEvidenceReport) -> str:
    """Plain-text rendering of the report block. Prints what the report holds; derives nothing."""
    h = report.header
    out = ["EVIDENCE REPORT  (facts for a human reviewer; no score, no ranking, no hire/reject signal)", "", "ANALYSED"]
    for a in h.analysed:
        out.append(f"  {a.get('repo', a.get('input'))}  status={a.get('status')}  ref={a.get('ref')}  head={str(a.get('head_sha'))[:12]}  "
                   f"scope={a.get('scope') or 'whole repo'}  source_files={a.get('source_files')}  "
                   f"source_lines={a.get('total_source_lines')}  languages={a.get('languages')}")
    out.append("EXCLUDED (never evidence)")
    for e in h.excluded:
        out.append(f"  {e['repo']}  files={e['files_by_reason'] or {}}  lines={e['lines_by_reason'] or {}}")
        for t in e["top_excluded"]:
            out.append(f"      {t}")
    out.append("AUTHORSHIP (ratio shown only with its confidence and reason)")
    for a in h.authorship:
        out.append(f"  {a['repo']}  authored={fmt_ratio(a['authored_ratio'])}  confidence={a['authorship_confidence']}  "
                   f"meaningful={a['meaningful']}  likely_identity_gap={a['likely_identity_gap']}")
        out.append(f"      reason: {a['reason']}")
    out.append("SHIELD")
    out.append(f"  {h.shield}" if h.shield else "  (no document analysed in this run)")
    out.append("NOT CHECKABLE FROM CODE")
    for n in h.not_checkable:
        out.append(f"  {n['claim_id']} [{n['kind']}]: {n['skip_reason']}")
    if not h.not_checkable:
        out.append("  (none)")
    out.append(f"GROUNDING FAILURES  {h.grounding_failures}")
    out.append("")
    out.append("CLAIMS")
    for r in report.rows:
        out.append(f"  {r.claim_id}  {r.verdict.upper():<13} {r.claim_text!r}  [{r.claim_kind}]")
        out.append(f"      reason: {r.reason}")
        if r.citation:
            c = r.citation
            lines = f":{c['start_line']}-{c['end_line']}" if c["start_line"] else ""
            out.append(f"      citation: {c['repo']} {c['path']}{lines}")
            out.append(f"      authorship: authored_by_candidate={r.authored_by_candidate}  file authored={fmt_ratio(r.authored_ratio)}  "
                       f"confidence={r.authorship_confidence}" + (f"  caveat: {r.authorship_caveat}" if r.authorship_caveat else ""))
            out.append(f"      confidence reason: {r.authorship_confidence_reason or NOT_MEASURED}")
        if r.authorship_note:
            out.append(f"      note: {r.authorship_note}")
        if r.grounding_checks:
            out.append(f"      grounding: passed={r.grounding_passed}  {r.grounding_checks}")
    return "\n".join(out)
