# Verdicts (Phase 4C)

A 4A claim and its 4B `EvidenceSet` become one `ClaimVerdict`: a row of the evidence table. There are no scores, no
ranking and no hire/reject signal.

```
python scripts/verdict_table.py RESUME --repo owner/name --user LOGIN [--runs 3] [--json out.json]
```

## The split

The model answers one narrow question: does this evidence support the CONTENT of the claim?
(`supported | contradicted | no_evidence | inconclusive`, with a citation.) Code then decides AUTHORSHIP from 4B's blame
data on the cited item (`backend/verdicts/matrix.py`). The model never receives `authored_by_candidate`,
`authored_ratio`, `authorship_confidence`, `likely_identity_gap`, `authorship_caveat` or the ranking score: the prompt
payload is built from an allow-list, and a test asserts this.

## Order of decisions (all in code)

1. **retrieval_performed False.** Verdict `unverifiable`, with 4B's skip_reason verbatim. No model call.
2. **Every named technology leaves no code artifact** (Linux, REST, ...). Verdict `inconclusive`, reason "this
   technology leaves no artifact in source code". No model call.
3. **No evidence items, but a populated search.** Verdict `no_evidence`. No model call: there is nothing for a model to
   read, and 4B's invariant already says this means "looked and found nothing". This is a deliberate deviation from
   "one call per claim where retrieval_performed is True", made to save calls that could only return one answer.
4. **Content judgement on Super.** Schema order: evidence_summary, content_reason, content_supported,
   cited_item_index. It is escalated once to Ultra when the re-ranked probability of the chosen label is below 0.70,
   or when the label is `contradicted`.
5. **Citation check.** `supported` / `contradicted` without a citation that resolves to an item becomes `inconclusive`,
   and the case is counted.
6. **The verdict matrix**, applied to the cited item, with the hard gates: authorship_confidence "none" -> never
   supported; likely_identity_gap -> never contradicted on authorship grounds.

## Scope rule (F1)

A contradiction requires the evidence and the claim to be about the SAME OBJECT. Crucible sees a partial sample of
repositories (public only, candidate-selected, unknown total), so absence in that sample is never evidence of absence.
A career, timeline or aggregate claim can never be contradicted by a repository sample.

Stage 5 sets `scope` deterministically (`backend/claims/routing.scope_of`): `specific_artifact` (a named file or
component), `project` (a named project), `career` (timeline, scale, years of experience), or `none`. `contradicted` is
permitted only for `specific_artifact` / `project` claims whose cited item belongs to that object; otherwise the verdict
is `inconclusive`, "evidence is not about the same scope as the claim". A history or count item can never contradict;
that is an explicit early check.

The failure that motivated it: "2014 - 2018" (a degree's dates) came back contradicted in 3 of 3 runs, citing one
repository's 2026 commit history.

## Ensemble (F2)

The content judgement is one request with n=3 samples at temperature 0.7. Each sample goes through the citation check
and the matrix; only 3 of 3 identical FINAL verdicts produce that verdict. Anything else is `inconclusive`, "verdicts
disagreed", and all three samples are recorded. Escalation to an Ultra ensemble (same rule) happens only after
unanimity: when the verdict is contradicted, or when a sample's chosen-label probability is below 0.70.

## Notes

- This replaces the earlier 4C judgement contract (`evidence_sufficient` with a code override). It reuses the
  distinct-first-character verdict enum and the sequence re-ranking.
- The ownership rule maps `ownership` claims to `contradicted` and every other kind to `supported` with an authorship
  note.
