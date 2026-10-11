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

## Phase 4J: make the report non-empty

**J1: skills lists decompose in code** (`backend/claims/skills.py`).
- Applies to a delimited list under a Skills / Technologies / Tools / Languages / Frameworks / Stack heading, or behind such a label ("Skills: Python, Docker.").
- It is split on `, ; |` and bullet glyphs at parenthesis depth 0, after stripping a leading category label. There is no model call.
- Each item becomes one `technology_use` claim with scope none, checkable yes, the bare item as its text, and a `source_span` on the item itself.
- At most 40 items per unit; the overflow is excluded as `list_cap` with the dropped items named.
- An item the tech lexicon doesn't know is skipped by 4B as `unknown_technology: …`, not dropped. An empty search would wrongly read as "looked, found nothing".

**J2: resume identity vs operator identity** (`backend/grounding/identity.py`).
- The resume's GitHub links (from 2C) are compared with `--user` / `--repo`. Statuses: `agree`, `mismatch`, `unconfirmed` (repository links only, none owned by the user), `no_link`.
- A mismatch shows at the top of the page and in every claim row's authorship cell.
- With no `--repo`, the resume's repositories are used, and the header says so.
- The run never fails on a mismatch.

**J3: career-scope timeline and scale claims never reach a model.**
- `routing.apply_scope` makes them not checkable (`career-scope … claim`), and 4B skips them again as defence in depth.
- A timeline or scale claim that names a file or a project is scoped to it and stays searchable.
- The 4C history/count gate is kept, and pinned by a test that forces evidence past routing with a mocked unanimous contradiction.

**J4: three smaller fixes, plus a pin.**
- **J4a:** job-title lines route to `role_or_team`.
- **J4b:** `location_kind` (page | docx_run | metadata, plus `document` for whole-document findings) is set by every 2C detector, and `page=0` no longer means "not a page".
- **J4c:** 3.6 keeps its unredacted `IdentitySet` as a private, never-serialized attribute (`identity_of`), so the identity is no longer rebuilt.
- **Pin:** the excerpt clone is pinned to the analysed `head_sha` (fetched explicitly if the branch moved). De-duplicating the double clone is left for later.

Recorded, not fixed: `route("Built the cache client")` is `outcome`, because the outcome pattern matches "client" as in customers.

## Phase 4K: correctness fixes

**K1: technology_use checks authorship.**

| Cited evidence | Verdict |
|---|---|
| technology in code the candidate authored | `supported` |
| technology present, not in authored code | `inconclusive`, "the technology is present but in code the candidate did not author" (`supported+not_authored+technology`) |
| `authored_by_candidate` is None (confidence none, file never blamed, identity not reproduced) | `inconclusive` |

- `artifact_exists` keeps the old rule (supported, with an authorship note), because "the repo has CI" is about the repository.
- 4D now runs `c_authored` on technology_use too. Ownership needs every source line of the cited span to be the candidate's; technology_use needs at least one.
- Every technology item carries `volume`, which is never sent to the model. For a language it is authored source lines in that language, of the repository's lines in it, the same lines 3.6 reports. For a library it is the files whose import line the candidate authored, of all files importing it.

**K2: the confidence ladder falls down, never up.** "7 commits by 2 authors, but over only 1.0 days" was `medium`. Several authors over less than 7 days is now `low`.

Audit of the other rungs:
- `none` (one commit; one author inside an hour): no hole.
- `low` (fewer than 5 commits; span under 1 day; clustered committer dates): no hole. Each rung is conservative.
- `medium` for a single author over any span of a day or more: never reaches high. Conservative, by design.
- **Open, author count:** it is distinct commit emails. One person committing from two addresses (a laptop with an unlinked email, plus the noreply address) counts as two authors, and so can reach `high` with no second person. Bots (dependabot, github-actions[bot]) count as authors too. Both push upward. Fixing them needs alias resolution, beyond `.mailmap`, and a bot list; recorded, not fixed.
- **Open, dates:** every span uses author dates, which the committer sets. The clustered-committer-date rung catches history rewrites, but not a repository whose author and committer dates were both forged. Version history cannot detect that.

**K3: fixture code is authored code.**
- Linguist's fixture patterns (`testdata/`, `tests?/fixtures/`, `specs?/fixtures/`, `.github/`) exclude files from language statistics. For a CODE file whose only vendor matches are those patterns, the file is now authorship-only.
- An authorship-only file is still excluded from line counts, language stats and the authored ratio, so those numbers are unchanged. It is blamed, usable as ownership and technology evidence, and listed in the report (`authorship_only_files`).
- Fixture data (pdf, json, docx …) and paths that also match real vendoring (`node_modules/`, `vendor/`) stay fully excluded.
- On hellstar-lab/Crucible the 74 vendored files are 71 under `tests/fixtures/`, plus `.gitignore`, one `node_modules/` file and one `vendor/` file (both inside the deliberately-vendored `exclusion_repo` fixture).
- The authored-code false positives were the three generators: `make_doc_fixtures.py` (45 KB), `make_fixtures.py` and `make_git_fixtures.py`. The other 7 code files sit inside `tests/fixtures/exclusion_repo/`, a fake repository built to look vendored, minified and duplicated. They are authored too and now authorship-only, except the two under `node_modules/` and `vendor/`.

**K4: presentation.**
- **K4a:** the identity-mismatch sentence appears once, in the banner. Rows and the Analysed section carry a short marker.
- **K4b:** every exclusion reason that reports files reports lines, 0 included. The 12 "duplicates" are empty `.gitkeep` files, 0 lines, measured.

**K5: a resume the repository can answer** (`scripts/make_crucible_resume.py` → `tests/fixtures/docs/docx_crucible_resume.docx`).

Three live runs against hellstar-lab/Crucible main @ 2bffef4. The identity check agreed, and the repository was taken from the resume.
- **Contradicted** ("Designed the unicode scanning module": `backend/shield/unicode_scan.py`, 164/164 lines by Auratech350) in all 3 runs, both tiers 3/3.
- **Supported ownership** ("Built the blame attribution module": `backend/evidence/blame.py`) in 2 runs. In run 3 that bullet was excluded at Stage 3 ("resolutions disagreed").
- **Skills:** 9 of 11 supported in every run, with volume. FastAPI is always inconclusive: the requirements.txt line is not the candidate's, and 0 files import it.
- **httpx** was supported in runs 1 and 3 and inconclusive in run 2, where the model cited the requirements.txt line, which another author wrote, instead of an import line the candidate wrote.

Findings:
1. **K1 makes technology verdicts depend on WHICH item the model cites** (httpx above). Never repairing citations is a rule, so the fix is a design choice: either code picks the technology item deterministically and the model judges content only, or the verdict stays citation-bound. Open.
2. **"Led a team of 4 engineers" is dropped at Stage 2 selection** ("no checkable fact") in all three runs, so it never reaches routing and appears as an excluded unit, not as unverifiable. The unverifiable rows that did appear are Stage 4 sub-claims routed `other`.
3. **Another authored-code false positive, in the GENERATED list:** `backend/evidence/exclusions.py`, the whole exclusion module (198 lines), is excluded as generated, because pygount's "do not edit" header check matched a docstring sentence that only mentions such headers. As a result no counted file imports `yaml`, and PyYAML shows "0 of 0 files". Not fixed: it needs the generated check restricted to comment headers.
4. Stage 4 sometimes splits a metric into fragments ("By 40%" as a claim, run 3); both pieces were no_evidence.
