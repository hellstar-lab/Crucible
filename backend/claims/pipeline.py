"""Phase 4A pipeline: document analyzer output -> atomic, traceable, typed claims. EXTRACTION ONLY, no verdicts.

    extract_claims(report, decomposition_tier="nano")      # report: backend.shield.analyze.analyze_document(...)

Order per unit: Stage 1 split (deterministic) -> Stage 2 selection -> Stage 3 disambiguation -> Stage 4
decomposition -> typed support checks -> Stage 5 routing (deterministic).

SHIELD GATE. Hostile resume text reaches a model here for the first time. The 2C structure block is built from
VISIBLE characters only (hidden, off-page and Unicode-smuggled text never enters it), so extraction always runs on
cleaned text. When the document carries any security finding or hidden fragment, the gate is recorded as applied
and every unit is additionally searched for the hidden fragments' text; a unit that still contains one is removed
before any model call. The Shield facts are carried into the output so the report shows both the claims and that
the document tried to manipulate its reader.

Every unit's ``source_span`` indexes into ``document_text`` and survives all stages; every claim carries its
unit's span. A claim that cannot be traced to a span is a bug.

Exclusions are correct outputs, not failures: no_verifiable_claims, cannot_be_disambiguated, unclear,
model_refused (G4), fabricated_quantity / fabricated_entity (typed support check), injection_suspected (a reply
that adds content not derivable from the input or returns instructions), qualifier_dropped (Stage 4 split a
quantity from the condition that limits it).

Stage 4's tier is an eval question and is passed explicitly; there is no default. The evidence_sufficient override
is NOT applied in 4A: it belongs only to the 4C judgement contract (judgement.py). Stage 3 sits behind a
deterministic pre-gate: a unit with no pronoun or definite reference is resolved without a model call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path

from backend import llm
from backend.contracts import (ClaimExtraction, ClaimUnit, DocumentReport, ExcludedUnit, ExtractedClaim, ShieldGate, ShieldItem,
                               StageDecision, SHIELD_SPAN_CAP)

from . import config, guards, routing, skills, stages, support
from .split import Unit, split_structure

log = logging.getLogger(__name__)

_INSTRUCTION = re.compile(r"(?i)\b(ignore (all |any )?(previous|prior|above) instructions|disregard|system prompt|you (must|should) "
                          r"(rate|hire|recommend|score|rank)|recommend (hiring|this candidate)|rate this candidate|hire this candidate|"
                          r"as the (evaluator|reviewer|recruiter))\b")
_QUALIFIER = re.compile(r"(?i)\b(on|for|in|under|during|at|across|within|per|when|while)\s+((?:the|a|an|our|its)\s+)?"
                        r"[\w./-]+(?:\s+(?!by\b|and\b|which\b|with\b|to\b)[\w./-]+){0,2}")


class _Budget(Exception):
    pass


BARE_DATE_REASON = "bare date range belongs to the timeline extractor"


def is_bare_date_range(text: str) -> bool:
    """True when the whole text is a date range ("2014 - 2018", "Jan 2020 - Present", "03/2020-12/2021"): a field value
    for 2C's timeline extractor, not a claim (Phase 4C F3). Deterministic."""
    from backend.shield import resume

    t = re.sub(r"^[\s(\[]+|[\s)\].,;:]+$", "", text or "")
    return bool(t) and resume._RANGE.fullmatch(t) is not None


def _span(text):
    if text is None:
        return None, False
    return text[:SHIELD_SPAN_CAP], len(text) > SHIELD_SPAN_CAP


def gate(report: DocumentReport) -> ShieldGate:
    detectors = sorted({f.detector for f in report.security_findings})
    items = []
    where = {"docx_run": "DOCX run (no fixed pages)", "metadata": "document metadata", "document": "document"}
    for h in report.hidden_fragments:
        span, cut = _span(h.text)
        items.append(ShieldItem(kind="hidden_fragment", detector=h.detection_method, reasons=list(h.reasons), page=h.page,
                                location_kind=h.location_kind,
                                location=f"page {h.page}" if h.location_kind == "page" else where[h.location_kind],
                                span=span, span_truncated=cut))
    for f in report.security_findings:
        span, cut = _span(f.reconstructed_text)
        items.append(ShieldItem(kind="security_finding", detector=f.detector, severity=f.severity, page=f.page,
                                location_kind=f.location_kind,
                                location=f"page {f.page}" if f.location_kind == "page" else where[f.location_kind],
                                span=span, span_truncated=cut))
    return ShieldGate(applied=bool(report.security_findings or report.hidden_fragments), document_status=report.status,
                      needs_review=report.needs_review, detectors=detectors, hidden_fragments=len(report.hidden_fragments),
                      items=items)


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def quantity_qualifiers(unit_text: str) -> dict[float, list[str]]:
    """For each quantity in the unit, the prepositional conditions that follow it in the same clause."""
    out: dict[float, list[str]] = {}
    for m in support._QUANTITY.finditer(unit_text):
        clause = re.split(r"[,;]|\s(?:and|by|which|while)\s", unit_text[m.end():], maxsplit=1)[0]
        quals = [q.group(0).strip() for q in _QUALIFIER.finditer(clause)]
        if quals:
            value = support.quantities(m.group(0))[0][1]
            out.setdefault(value, []).extend(quals)
    return out


def dropped_qualifiers(claim_text: str, claim_qualifiers: list[str], unit_text: str) -> list[str]:
    """Conditions attached to a quantity in the unit that a claim containing that quantity no longer carries."""
    attached = quantity_qualifiers(unit_text)
    have = {support.stem(w) for w in re.findall(r"[A-Za-z0-9]+", claim_text + " " + " ".join(claim_qualifiers))}
    missing = []
    for _w, value in support.quantities(claim_text):
        for q in attached.get(value, []):
            words = [support.stem(w) for w in re.findall(r"[A-Za-z0-9]+", q) if w.casefold() not in support.STOPWORDS]
            if words and not all(w in have for w in words):
                missing.append(q)
    return missing


class StageCache:
    """Record Stage 2 and Stage 3 results once, replay them identically (S7: the Stage 4 tier comparison).

    Only Stage 4's tier may vary between arms; a comparison in which Stages 2-3 re-run per arm is confounded (nano
    showed 3-pass / 2-fail variance at temperature 0). In ``record`` mode every selection/disambiguation result is
    stored under sha256(stage, text, context); in ``replay`` mode no Stage 2/3 call is made and a missing entry is an
    error, never a silent fresh call. ``dumps`` is canonical (sorted keys), so a replayed cache re-serialises to the
    exact bytes it was loaded from.
    """

    STAGES = ("selection", "disambiguation")

    def __init__(self, mode: str, entries: dict | None = None):
        if mode not in ("record", "replay"):
            raise ValueError(mode)
        self.mode = mode
        self.entries: dict[str, dict] = dict(entries or {})
        self.replayed: dict[str, dict] = {}

    @staticmethod
    def key(stage: str, text: str, context: str) -> str:
        return hashlib.sha256(f"{stage}\x00{text}\x00{context}".encode("utf-8")).hexdigest()

    def get(self, stage: str, text: str, context: str) -> stages.StageResult:
        k = self.key(stage, text, context)
        if k not in self.entries:
            raise KeyError(f"replay cache has no {stage} result for this unit; record it first")
        self.replayed[k] = self.entries[k]
        return stages.StageResult(**self.entries[k])

    def put(self, stage: str, text: str, context: str, r: stages.StageResult) -> None:
        self.entries[self.key(stage, text, context)] = r.to_json()

    @staticmethod
    def dumps(entries: dict) -> str:
        return json.dumps(entries, sort_keys=True, ensure_ascii=False, indent=1) + "\n"

    def save(self, path) -> None:
        Path(path).write_text(self.dumps(self.entries), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "StageCache":
        return cls("replay", json.loads(Path(path).read_text(encoding="utf-8")))


class _Run:
    def __init__(self, document_text: str, tiers: dict[str, str], cap: float, run_tag: str | None, cache: StageCache | None = None,
                 escalation: str | None = None):
        self.text = document_text
        self.tiers = tiers
        self.cap = cap
        self.run_tag = run_tag
        self.cost = 0.0
        self.cost_known = True
        self.calls = 0
        self.counters: Counter = Counter()
        self.key_orders: list[bool] = []
        self.decisions = 0
        self.cache = cache
        self.escalation = escalation  # Stage 3 escalation tier (ultra), off by default
        self.stage3_n = config.STAGE3_ENSEMBLE_N  # P2-3: 1 = single call, >1 = ensemble with unanimity
        self.reason_first = config.REASON_FIRST  # C2: reason before verdict by default (verdict-first kept for A/B)
        self.claims: list[ExtractedClaim] = []
        self.excluded: list[ExcludedUnit] = []
        self.units: list[ClaimUnit] = []
        self.truncated, self.truncation_reason = False, None
        self.current_stage = "selection"  # the stage whose call is in flight, for attributing a failed call

    def exclude(self, u: Unit, stage: str, code: str, detail: str):
        self.excluded.append(ExcludedUnit(unit_id=u.unit_id, source_span=(u.start, u.end), source_section=u.section, stage=stage,
                                          reason_code=code, detail=detail[:500]))

    def stage(self, fn, cu: ClaimUnit, text: str, context: str, tier: str):
        name = {stages.select: "selection", stages.disambiguate: "disambiguation", stages.disambiguate_ensemble: "disambiguation",
                stages.decompose: "decomposition"}[fn]
        self.current_stage = name
        if self.cache is not None and self.cache.mode == "replay" and name in StageCache.STAGES:
            r = self.cache.get(name, text, context)  # no call, no cost: identical input to every Stage 4 arm
        else:
            if self.cost >= self.cap:
                raise _Budget(f"cost cap ${self.cap:.6f} reached after {self.calls} calls")
            self.calls += 1
            r = fn(text, context, tier, run_tag=self.run_tag, reason_first=self.reason_first)
            if r.cost_usd is None:
                self.cost_known = False
            self.cost += r.cost_usd or 0.0
            self.counters["sentinel_normalized"] += r.sentinel_normalized
            if r.greedy_disagreement:
                self.counters["greedy_disagreement"] += 1
            if self.cache is not None and self.cache.mode == "record" and name in StageCache.STAGES:
                self.cache.put(name, text, context, r)
        self.decisions += 1
        if r.key_order is not None:
            self.key_orders.append(r.key_order == list(stages.schema_for(name, self.reason_first)["properties"]))
        cu.decisions.append(StageDecision(stage=name, tier=r.tier, model_verdict=r.verdict, verdict=r.verdict or "missing",
                                          reason=r.data.get("reason"), key_order=r.key_order, prompt_hash=r.prompt_hash,
                                          cost_usd=r.cost_usd, reply_sha256=r.reply_sha256,
                                          value_distribution=(r.value_distributions or {}).get("verdict"),
                                          greedy_disagreement=r.greedy_disagreement, samples=r.samples))
        return r

    @staticmethod
    def check_output(produced: str, doc_source: str, unit_text: str, support_source: str | None = None) -> tuple[str, str] | None:
        """(reason_code, detail) for the first guard the model-produced text fails, or None.
        doc_source: everything the document gave the stage (unit + context); unit_text: the unit alone;
        support_source: what the typed support check compares against (defaults to doc_source)."""
        if _INSTRUCTION.search(produced):
            return "injection_suspected", f"the reply contained instructions rather than content: {produced[:200]!r}"
        leak = guards.rubric_leak(produced, doc_source)
        if leak:
            return "rubric_leak", f"copied from rubric example {leak[0]!r} (shared phrase {leak[1]!r}): {produced[:200]!r}"
        ungrounded = guards.ungrounded_qualifiers(produced, unit_text)
        if ungrounded:
            return "ungrounded_qualifier", f"{ungrounded} not in the source unit: {produced[:200]!r}"
        chk = support.check(produced, support_source if support_source is not None else doc_source)
        if chk.hard_fail:
            code = "fabricated_quantity" if chk.fabricated_quantities else "fabricated_entity"
            return code, f"not in the source: quantities={chk.fabricated_quantities} entities={chk.fabricated_entities}; text={produced[:200]!r}"
        return None

    def fail(self, u: Unit, stage: str, code: str, detail: str) -> None:
        self.counters[code] += 1
        if code in ("fabricated_quantity", "fabricated_entity"):
            self.counters["injection_suspected"] += 1  # content not derivable from the input (rule 3)
        self.exclude(u, stage, code, detail)

    def derivable(self, u: Unit, cu: ClaimUnit, produced: str, doc_source: str, stage: str, support_source: str | None = None) -> bool:
        """Every guard on model-produced text. False -> the unit is excluded and the guard's counter increments."""
        hit = self.check_output(produced, doc_source, u.text, support_source)
        if hit:
            self.fail(u, stage, *hit)
            return False
        return True

    def _stage3_problem(self, r, selected: str, source: str, u: Unit) -> tuple[str, str] | None:
        if r.verdict == "cannot_be_disambiguated":
            return "cannot_be_disambiguated", f"failed referent: {r.data.get('failed_referent')!r}; {r.data.get('reason') or ''}"
        if r.verdict != "resolved":
            return "unparseable_response", f"no valid disambiguation label: {r.verdict!r}"
        return self.check_output(r.data.get("clarified_text") or selected, source, u.text)

    def skills_list(self, u: Unit, cu, items):
        kept, over = items[: skills.MAX_ITEMS], items[skills.MAX_ITEMS:]
        self.counters["skills_units"] += 1
        self.counters["skills_claims"] += len(kept)
        cu.decisions.append(StageDecision(stage="decomposition", tier="deterministic", verdict="decomposed",
                                          reason=f"skills list split in code into {len(kept)} item(s); no model call"))
        checkable, evidence, why = routing.ROUTES["technology_use"]
        for i, (a, b, item) in enumerate(kept):
            self.claims.append(ExtractedClaim(
                claim_id=f"{u.unit_id}.c{i}", unit_id=u.unit_id, text=item, qualifiers=[], source_span=(a, b),
                source_section=u.section, kind="technology_use", secondary_kinds=[], checkable_from_code=checkable,
                evidence_kind=evidence, why_not_checkable=why, scope="none", scope_target=None, decomposition_tier="deterministic"))
        if over:
            self.counters["skills_items_over_cap"] += len(over)
            self.exclude(u, "decomposition", "list_cap", f"skills list has {len(items)} items; the first {skills.MAX_ITEMS} became claims, "
                         f"these {len(over)} did not: {', '.join(t for _a, _b, t in over)}")
        return None

    def process(self, u: Unit):
        cu = u.to_contract()
        self.units.append(cu)
        context = u.context()
        source = f"{u.text}\n{context}"
        if is_bare_date_range(u.text):  # F3: never reaches a model, 4B or 4C
            self.counters["bare_date_units"] += 1
            return self.exclude(u, "selection", "no_verifiable_claims", BARE_DATE_REASON)
        if skills.is_skills_unit(u.text, u.section, u.heading_path):  # J1: a list decomposes in code, no model call
            items = skills.split_items(self.text, u.start, u.end)
            if items is not None:
                return self.skills_list(u, cu, items)
        try:
            # Stage 2: selection
            r = self.stage(stages.select, cu, u.text, context, self.tiers["selection"])
            if r.verdict not in ("has_verifiable_content", "no_verifiable_claims", "mixed"):
                return self.exclude(u, "selection", "unparseable_response", f"no valid selection label in the reply: {r.verdict!r}")
            if r.verdict == "no_verifiable_claims":
                return self.exclude(u, "selection", "no_verifiable_claims", r.data.get("reason") or "")
            selected = u.text
            if r.verdict == "mixed":
                selected = r.data.get("verifiable_text")
                if not selected:
                    return self.exclude(u, "selection", "unparseable_response", "labelled mixed but verifiable_text was null")
                if not self.derivable(u, cu, selected, source, "selection", support_source=u.text):
                    return None
                cu.dropped_text = r.data.get("dropped_text")
            cu.selected_text = selected

            # Stage 3: disambiguation, behind a deterministic pre-gate (S2)
            trigger = stages.needs_disambiguation(selected)
            if trigger is None:
                self.counters["stage3_pregate_skips"] += 1
                cu.decisions.append(StageDecision(stage="disambiguation", tier="pre-gate", verdict="resolved",
                                                  reason="no pronoun or definite reference: nothing to resolve, no model call"))
                clarified = selected
            else:
                fn3 = stages.disambiguate_ensemble if self.stage3_n > 1 else stages.disambiguate
                r = self.stage(fn3, cu, selected, context, self.tiers["disambiguation"])
                if r.samples is not None and r.data.get("reason") == "resolutions disagreed":
                    self.counters["stage3_ensemble_disagreements"] += 1
                hit = self._stage3_problem(r, selected, source, u)
                if hit and hit[0] not in ("cannot_be_disambiguated", "unparseable_response") and self.escalation:
                    # super was caught guessing (a guard fired on its rewrite): ask the escalation tier once
                    self.counters["stage3_escalations"] += 1
                    r = self.stage(fn3, cu, selected, context, self.escalation)
                    hit = self._stage3_problem(r, selected, source, u)
                if hit:
                    if hit[0] in ("cannot_be_disambiguated", "unparseable_response"):
                        return self.exclude(u, "disambiguation", *hit)
                    return self.fail(u, "disambiguation", *hit)
                clarified = r.data.get("clarified_text") or selected
            cu.clarified_text = clarified

            # Stage 4: decomposition
            r = self.stage(stages.decompose, cu, clarified, context, self.tiers["decomposition"])
            raw_claims = [c for c in (r.data.get("claims") or []) if isinstance(c, dict) and c.get("text")]
            if r.verdict not in ("decomposed", "no_verifiable_claims"):
                return self.exclude(u, "decomposition", "unparseable_response", f"no valid decomposition label: {r.verdict!r}")
            if r.verdict == "no_verifiable_claims" or not raw_claims:
                return self.exclude(u, "decomposition", "no_verifiable_claims", r.data.get("reason") or "decomposition yielded no claims")
            # Stage 4 claims must come from THIS unit: the source is the unit and its Stage 3 output, not the wider
            # context. (Measured: a role line was given "Migrated ... to Kubernetes", which only the next bullet says.)
            unit_source = f"{u.text}\n{clarified}"
            for c in raw_claims:
                if not self.derivable(u, cu, c["text"] + " " + " ".join(c.get("qualifiers") or []), source, "decomposition",
                                      support_source=unit_source):
                    return None  # rule 3: the whole unit is excluded
            kept = []
            for c in list(raw_claims):
                if is_bare_date_range(c["text"]):  # F3: a date range produced by decomposition is a field value too
                    self.counters["bare_date_claims_dropped"] += 1
                    cu.dropped_claims.append(c["text"])
                    raw_claims.remove(c)
            if not raw_claims:
                return self.exclude(u, "decomposition", "no_verifiable_claims", BARE_DATE_REASON)
            for c in raw_claims:
                missing = dropped_qualifiers(c["text"], [q for q in (c.get("qualifiers") or []) if q], u.text)
                if missing:
                    self.counters["qualifier_dropped"] += 1
                    self.exclude(u, "decomposition", "qualifier_dropped", f"claim {c['text']!r} lost {missing}")
                    continue
                kept.append(c)
            for i, c in enumerate(kept):
                rt = routing.route(c["text"])
                scope, target = routing.scope_of(c["text"], rt["kind"], u.text, u.section)
                rt = routing.apply_scope(rt, scope)  # J3: career-scope timeline / scale never reach retrieval or a model
                self.claims.append(ExtractedClaim(scope=scope, scope_target=target, 
                    claim_id=f"{u.unit_id}.c{i}", unit_id=u.unit_id, text=c["text"], qualifiers=[q for q in (c.get("qualifiers") or []) if q],
                    source_span=(u.start, u.end), source_section=u.section, kind=rt["kind"], secondary_kinds=rt["secondary_kinds"],
                    checkable_from_code=rt["checkable_from_code"], evidence_kind=rt["evidence_kind"], why_not_checkable=rt["why_not_checkable"],
                    checkability_note=rt["checkability_note"], support_ratio=support.claim_support_ratio(c["text"], unit_source),
                    decomposition_tier=self.tiers["decomposition"]))
        except llm.LLMRefusalError as exc:
            self.counters["model_refused"] += 1
            where = exc.refusal if isinstance(exc.refusal, dict) else {"kind": "api_refusal_field"}
            self.exclude(u, self.current_stage, "model_refused", f"refusal: {where}")
        except llm.LLMJSONError as exc:
            self.exclude(u, self.current_stage, "unparseable_response",
                         f"reply was not JSON ({len(getattr(exc, 'raw_text', '') or '')} chars; a reply that hits max_tokens is cut mid-JSON)")
        return None


def _finish(run: _Run, document: str, gate_info: ShieldGate, extra_excluded: list[ExcludedUnit]) -> ClaimExtraction:
    run.excluded = extra_excluded + run.excluded
    counters = dict(run.counters)
    for k in ("model_refused", "sentinel_normalized", "injection_suspected", "fabricated_quantity", "fabricated_entity",
              "qualifier_dropped", "stage3_pregate_skips", "rubric_leak", "ungrounded_qualifier", "stage3_escalations",
              "greedy_disagreement", "stage3_ensemble_disagreements", "bare_date_units", "bare_date_claims_dropped",
              "skills_units", "skills_claims", "skills_items_over_cap"):
        counters.setdefault(k, 0)
    counters["model_decisions"] = run.decisions
    return ClaimExtraction(
        document=document, document_text=run.text, shield=gate_info, units=run.units, claims=run.claims, excluded=run.excluded,
        counters=counters, tiers=run.tiers, calls=run.calls, cost_usd=round(run.cost, 8) if run.cost_known else None,
        cost_cap_usd=run.cap, truncated=run.truncated, truncation_reason=run.truncation_reason,
        sufficiency_disagreement_rate=None,  # the judgement contract (and its override) is 4C only; not measured in 4A
        key_order_follows_schema_rate=round(sum(run.key_orders) / len(run.key_orders), 4) if run.key_orders else None)


def _run_units(units: list[Unit], document_text: str, tiers: dict[str, str], cap: float, run_tag: str | None,
               cache: StageCache | None = None, escalation: str | None = None, stage3_n: int | None = None,
               reason_first: bool = config.REASON_FIRST) -> _Run:
    run = _Run(document_text, tiers, cap, run_tag, cache, escalation)
    if stage3_n is not None:
        run.stage3_n = stage3_n
    run.reason_first = reason_first
    for i, u in enumerate(units):
        try:
            run.process(u)
        except (_Budget, llm.LLMBudgetError) as exc:
            run.truncated, run.truncation_reason = True, str(exc)
            for rest in units[i:]:
                if not any(x.unit_id == rest.unit_id for x in run.units):
                    run.units.append(rest.to_contract())
                run.exclude(rest, "budget", "cost_cap", "not processed: per-document cost cap reached")
            break
        except llm.LLMUnavailableError as exc:
            run.truncated, run.truncation_reason = True, f"model unavailable: {exc}"
            for rest in units[i:]:
                run.exclude(rest, "budget", "model_unavailable", "not processed: the model could not be reached")
            break
    return run


def extract_claims(report: DocumentReport, *, decomposition_tier: str, selection_tier: str = "nano",
                   disambiguation_tier: str = config.STAGE3_TIER, stage3_escalation: str | None = None,
                   cost_cap_usd: float | None = None, run_tag: str | None = None) -> ClaimExtraction:
    tiers = {"selection": selection_tier, "disambiguation": disambiguation_tier, "decomposition": decomposition_tier}
    cap = config.DEFAULT_COST_CAP_USD if cost_cap_usd is None else cost_cap_usd
    g = gate(report)
    if report.structure is None:
        return ClaimExtraction(document=report.document.path, shield=g, tiers=tiers, cost_cap_usd=cap,
                               not_extracted_reason="the document analyzer produced no text structure (image-only, encrypted or over a cap)")
    document_text, units, contact = split_structure(report.structure)
    pre: list[ExcludedUnit] = [ExcludedUnit(unit_id=u.unit_id, source_span=(u.start, u.end), source_section=u.section, stage="split",
                                            reason_code="contact_info", detail="contact details are never sent to a model")
                               for u in contact]
    if g.applied:
        payloads = [_norm(f.text) for f in report.hidden_fragments if len(f.text.strip()) >= config.FRAGMENT_MIN_CHARS]
        kept = []
        for u in units:
            if any(p and p in _norm(u.text) for p in payloads):
                pre.append(ExcludedUnit(unit_id=u.unit_id, source_span=(u.start, u.end), source_section=u.section, stage="shield",
                                        reason_code="shield_payload", detail="the unit contains text Shield found hidden"))
            else:
                kept.append(u)
        g.units_removed = len(units) - len(kept)
        units = kept
    run = _run_units(units, document_text, tiers, cap, run_tag, escalation=stage3_escalation)
    out = _finish(run, report.document.path, g, pre)
    out.units = [x.to_contract() for x in contact] + out.units
    return out


def extract_from_text(text: str, *, headings: list[str] | None = None, before: list[str] | None = None, decomposition_tier: str,
                      selection_tier: str = "nano", disambiguation_tier: str = config.STAGE3_TIER, stage3_escalation: str | None = None,
                      cost_cap_usd: float | None = None, run_tag: str | None = None, cache: StageCache | None = None,
                      stage3_n: int | None = None, reason_first: bool = config.REASON_FIRST) -> ClaimExtraction:
    """For the eval set: one section of plain text (bullets or prose) with optional headings / preceding lines as context."""
    from .split import split_lines

    lines, pos = [], 0
    for line in text.split("\n"):
        lines.append((pos, pos + len(line), line))
        pos += len(line) + 1
    units = split_lines(lines, text, section=(headings or [None])[-1], heading_path=headings or [])
    for u in units:
        u.context_before = list(before or []) + u.context_before
    tiers = {"selection": selection_tier, "disambiguation": disambiguation_tier, "decomposition": decomposition_tier}
    cap = config.DEFAULT_COST_CAP_USD if cost_cap_usd is None else cost_cap_usd
    run = _run_units(units, text, tiers, cap, run_tag, cache, stage3_escalation, stage3_n, reason_first)
    return _finish(run, "<text>", ShieldGate(applied=False, document_status="clean"), [])
