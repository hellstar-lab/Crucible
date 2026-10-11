"""Phase 4B: evidence retrieval for extracted claims. Deterministic, zero model calls, no network.

    retrieve(claim, views)            -> EvidenceSet for one claim (backend.contracts)
    retrieve_all(claims, views)       -> one EvidenceSet per claim

Input: a 4A ExtractedClaim (kind, checkable_from_code, text, ...) and one RepoView per analysed repository
(sources.load_view: the 3.6 TargetReport + its cached snapshot + an optional checkout for excerpts).

INVARIANTS (see backend.contracts.EvidenceSet):
  * items == [] with ``searched`` populated means WE LOOKED AND FOUND NOTHING; ``searched`` None means WE DID NOT LOOK.
  * authored_by_candidate is decided here from the data and never inferred later. A technology existing in a
    repository is not evidence the candidate used it.
  * Excluded code (vendored, generated, minified, documentation, binary, duplicate) and out-of-scope paths are never
    evidence: every item is checked by sources.assert_eligible before it is returned.
Kinds role_or_team, outcome and other are not searched: retrieval_performed is False with a skip_reason.
"""

from __future__ import annotations

from datetime import date

from backend.contracts import EvidenceSet, SearchedBlock

from . import config, lexicon, rank, rules
from .sources import RepoView, assert_eligible, load_view  # noqa: F401  (re-exported)


def _claim_fields(claim) -> dict:
    get = (lambda k: claim.get(k)) if isinstance(claim, dict) else (lambda k: getattr(claim, k, None))
    return {k: get(k) for k in ("claim_id", "kind", "checkable_from_code", "text", "secondary_kinds", "scope",
                                "why_not_checkable")}


def _skip(c, version, reason) -> EvidenceSet:
    return EvidenceSet(claim_id=c["claim_id"], claim_kind=c["kind"], retrieval_performed=False, retrieval_version=version,
                       skip_reason=reason)


def retrieve(claim, views: list[RepoView], today: date | None = None) -> EvidenceSet:
    c = _claim_fields(claim)
    version = config.retrieval_version()
    kind = c["kind"]
    if kind in config.SKIPPED_KINDS:
        return _skip(c, version, config.SKIPPED_KINDS[kind])
    if kind in ("timeline", "scale"):  # J3 defence in depth: a career-scope claim is never searched, whoever built it
        from backend.claims.routing import CAREER_REASON, scope_of
        scope = c["scope"] or scope_of(c["text"] or "", kind)[0]
        if scope == "career":
            return _skip(c, version, CAREER_REASON.format(kind=kind))
    if c["checkable_from_code"] == "no" or kind not in config.KIND_RULES:
        return _skip(c, version, c["why_not_checkable"] or f"{kind} has no retrieval rule; not searched")
    if kind == "technology_use" and not lexicon.recognise(c["text"]):
        # J1: a technology the lexicon does not know is reported, not dropped: there is no manifest, import, config or
        # file pattern to search for, and an empty search would wrongly read as "looked and found nothing".
        return _skip(c, version, f"unknown_technology: {c['text']!r} is not in the technology lexicon, so there is no "
                                 "dependency, import, config or file pattern to search for")
    rule_names = list(config.KIND_RULES[kind])
    for k in c.get("secondary_kinds") or []:  # e.g. a metric claim that also names a technology
        for r in config.KIND_RULES.get(k, ()):
            if r not in rule_names:
                rule_names.append(r)
    techs = lexicon.recognise(c["text"])
    searched = SearchedBlock(technologies=techs)
    items, notes = [], []
    for view in views:
        searched.repos.append(view.full_name)
        searched.source_files += len([p for p in view.counted if view.eligible(p)])
        searched.excluded_files_skipped += len(view.excluded_paths)
        notes.extend(view.notes)
        for name in rule_names:
            fn = rules.RULES[name]
            if name == "r_scale":
                res = fn(c["text"], techs, view, all_views=views)
            elif name == "r_history":
                res = fn(c["text"], techs, view, today=today)
            else:
                res = fn(c["text"], techs, view)
            assert_eligible(view, res.items)
            items.extend(res.items)
            searched.manifests.extend(m for m in res.manifests if m not in searched.manifests)
            searched.structure_keys.extend(k for k in res.structure_keys if k not in searched.structure_keys)
            for marker in [name] + res.outcomes:
                if marker not in searched.rules_fired:
                    searched.rules_fired.append(marker)
        notes.extend(n for n in view.notes if n not in notes)
    ranked, dropped = rank.rank(c["text"], items)
    return EvidenceSet(claim_id=c["claim_id"], claim_kind=kind, retrieval_performed=True, searched=searched, items=ranked,
                       items_dropped=dropped, retrieval_version=version, notes=list(dict.fromkeys(notes)))


def retrieve_all(claims, views: list[RepoView], today: date | None = None) -> list[EvidenceSet]:
    return [retrieve(c, views, today=today) for c in claims]
