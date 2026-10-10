# Grounding check and report (Phase 4D)

The last gate before a verdict reaches a human. Deterministic: no model calls.

```
python scripts/live_grounding_demo.py --runs 3      # clean_small, live 4C, then 4D
```

## The four checks (`backend/grounding/__init__.py`)

Every verdict whose content label is supported or contradicted is re-checked against the data, never against the
model's words:

| check | passes when |
|---|---|
| `c_index` | the cited index resolves to an item of this claim's EvidenceSet |
| `c_path` | the item's path exists in the analysed tree (3.6 snapshot at head_sha) |
| `c_lines` | 1 <= start <= end <= file length; an item with no line range records `not_applicable`, never `pass` |
| `c_authored` | supported verdict on an ownership claim: the candidate authored EVERY source line of the cited range (the whole file when there is no range) |

- **Unavailable counts as a failure.** A check that cannot be performed (no snapshot, no identity, unknown length)
  records `unavailable`, and the verdict is not grounded.
- **Failures downgrade.** Any failure turns the verdict into `inconclusive`. The reason names the check verbatim, the
  original verdict is kept in `pre_grounding_verdict`, and `grounding_failures[check]` increments.
- **No repair.** A citation is never repaired.

## Strict scope gate (G2)

A contradiction requires the claim's scope target to appear in the cited item as a WHOLE path component or a WHOLE
word of its path or function name (`src/ledger/`, `ledger.py`, `reconcile_ledger`). No fuzzy score qualifies; the best
near-miss score is recorded for audit (`scope_match_rule`, `scope_match_score`).

Retrieval may be generous; contradiction must be strict. A missed contradiction is recoverable; a false contradiction
about a real person is not.

Under the strict rule the Bob's-ledger contradiction would have failed: "ledger reconciliation" requires the whole word
"reconciliation", and the code only has `reconcile_ledger`. The EXTRACTION was fixed, not a cutoff. For a component
phrase, `scope_of` now returns the first distinctive word ("ledger"): in "ledger reconciliation module" the first word
names the object, and the later words describe what is done to it.

## Report (`backend/grounding/report.py`)

`ClaimEvidenceReport` has one row per claim (verdict, reason, citation, authorship figure with its caveat and
confidence, grounding) and a header (what was analysed, what was excluded and why, per-repo authorship with its reason,
the Shield findings, claims not checkable from code, grounding failures). Every field is copied from the stage that
produced it, and the row's `sources` names that stage; nothing is computed for the first time. It is named
`ClaimEvidenceReport` because `EvidenceReport` is the frozen Phase 3 contract, whose `Finding.confidence` float the
no-scores rule forbids.

## Phase 4H: review fixes and the report page

**H0a: contradiction needs provenance, not word order.** An ownership claim can be `contradicted` only when all three hold:
- (1) the scope is `specific_artifact` or `project`;
- (2) the cited item came from `r_ownership`, the 4B rule that identified the artifact;
- (3) ANY content word of `scope_target` matches as a whole word or path component.

`scope_target` is now the whole component phrase ("ledger reconciliation"). The matched word and rule are recorded, e.g. `r_ownership:whole_path_component:ledger`.

The old first-distinctive-word heuristic read the object off word order. Whole-word matching still blocks the three motivating examples, so they become missed contradictions (the safe side), and tests pin that:
- "payment reconciliation service" vs `reconciler.py`;
- "user authentication module" vs `auth.py`;
- "data migration script" vs `migrate.py`.

**H0b: disagreement escalates.**
- Super n=3 unanimous → that verdict.
- Not unanimous → Ultra n=3, and Ultra's verdict if unanimous.
- Otherwise `inconclusive`, with both sample sets kept.
- Not a majority vote.

New counters: `escalations_on_disagreement`, `disagreements_resolved_by_ultra`, `escalations_on_check`.

Live (clean_small, 3 runs): 2 of 9 judged claim-runs escalated on disagreement, and Ultra was unanimous on both. c0 contradicted 3/3 runs, c1 supported 3/3, c2 supported 3/3, c3 unverifiable 3/3. In G5 before this fix, 3 of the 6 judged ownership runs had come out inconclusive.

**H0c: report hygiene.**
- Authored ratio as a percentage with at most one decimal (`fmt_ratio`).
- The 3.6 confidence reason copied onto every cited row.
- Zero-line languages dropped.
- `grounding_failures` lists every check (0 included) when grounding ran, and is `None` when it did not.

**The page.** `scripts/render_report.py REPORT.json` → `reports/<name>.html` (renderer: `backend/grounding/html.py`).
- Every value is emitted as `<span data-src="/json/pointer" data-fmt=…>`. `assert_traceable` re-parses the page and fails the render if any digit sits outside such a span, or a span's text differs from its formatted field. The renderer therefore cannot calculate.
- `assert_self_contained` fails on any fetchable reference.
- `scripts/degraded_report_fixture.py` builds the degraded report: real repos, retrieval, matrix and grounding, with scripted judgements and Shield result. The page says so in its provenance banner.

## Phase 4I: end to end, contract fixes

**I1: a contradiction needs both tiers.**
- `contradicted` requires Super 3/3 AND Ultra 3/3 on the FINAL verdict, after the matrix and blame.
- Any other route to contradicted is `inconclusive` (`contradiction:not_confirmed`), with both sample sets on the row.
- Counters: `contradictions_confirmed` and `contradictions_not_confirmed`.

Stance: *Crucible never accuses on thin evidence. Either two independent models agree and deterministic blame agrees, or the claim becomes a question for the interview.* The second half depends on Phase 4G existing.

Live, clean_small, two sets of 3 runs, claim c0:
- The first set gave one near miss: Super gave inconclusive / contradicted / inconclusive, then Ultra 3/3. That is now inconclusive. The other two runs were confirmed.
- The second set confirmed the contradiction in all 3 runs.

**I2: four contract fixes.**
- **I2a:** `superseded_reason` (a grounding downgrade keeps the reason it replaced).
- **I2b:** `header.candidate`, holding what the operator supplied. An email is refused.
- **I2c:** `ShieldGate.items`, each with kind, detector, reasons, page and location, plus the span capped at 2000 characters. A DOCX run has page None ("DOCX run (no fixed pages)"), because 2C records 0 there and 0 means metadata in the contract.
- **I2d:** not-checkable text comes from the claim.

**I3: `scripts/crucible.py`, one command.**
- Runs resume → 2C → Shield gate → 4A → 3.6 → 4B → 4C → 4D → page, with no hand-written claims.
- Prompt audit: every outgoing prompt is scanned in-process for each hidden span and the canary. `logs/model_calls.jsonl` stores prompt hashes, so grepping it can never fail.

**Glue the per-stage tests did not reveal** (see the 4I report for the runs):
1. Skills lists ("Python, Go, PostgreSQL, Docker, Kubernetes") are dropped at Stage 4 as `no_verifiable_claims`. They are the most code-checkable lines on both fixture resumes, and 4B never sees them.
2. A role line ("Senior Engineer, Northwind Payments") routes to `other`. 4B then reports "no checkability rule matched" instead of a role_or_team reason.
3. An education date range ("between 2014 and 2018") routes to `timeline` and is searched against repository history. Super said contradicted 3/3; only the history/count gate stopped it.
4. The `grep logs/model_calls.jsonl` injection check was vacuous (it can only see hashes). Replaced by the in-process audit.
5. 2C writes `page=0` for DOCX hidden runs, which the contract defines as "document metadata".
6. 3.6 clones each repo and 4B clones it again for excerpts. The second clone can miss `head_sha` if the branch moves; `load_view` records that.
7. The candidate identity is rebuilt outside 3.6 (3.6 redacts emails), duplicating its precedence logic and its GitHub API calls.
8. Nothing compares the operator-supplied `--user` with the resume's own GitHub link. The clean fixture links `github.com/jordan-lee-demo` and was run as `hellstar-lab`, with no warning.
