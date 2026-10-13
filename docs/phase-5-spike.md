# Phase 5 spike: bug injection, measured (2026-10-11)

Measurement only. Nothing under `backend/` changed. Scratch code is in `scripts/spike/`, which is throwaway; every
mutation was reverted, and the tree was verified clean after each one.

| Question | Verdict |
|---|---|
| Q1: what does validation cost? | **GO** |
| Q2: how many valid bugs exist? | **NEEDS-REDESIGN**: most clean kills telegraph |
| Q3: is the AI stranger baseline fair? | **NEEDS-REDESIGN**: neither condition is a fair baseline as built |

## Read this first: the target is self-referential

Every number comes from the Crucible repository itself, at the local HEAD of `phase-4-claims`. It is Python, with
863 tests and blame data. A real candidate repository will differ, in three ways that matter here:

1. **Test density and naming.** Crucible's tests were written alongside its code, densely, with long descriptive
   names (`test_display_box_handles_offset_origin_and_rotation`). That makes kills likely and telegraphing likely.
   A typical candidate repository has fewer tests, so it will produce fewer kills. Whether it telegraphs less is
   unknown.
2. **"Authored" is an identity, not a person.** Most of this repository was written in Claude Code sessions and
   committed under `hellstar-lab`. Filter (a) means "committed under the candidate's identity", not "hand-written".
   On Crucible, 21,939 of 23,899 candidates (92%) pass it. That says nothing about how a real candidate repository
   would filter.
3. **Size.** 73 backend files and 8,601 statements: larger than most candidate repositories.

## Setup

- The spike venv lives outside the repo (the session scratchpad), on Python 3.11.17. The project's installed
  packages are visible read-only through a `.pth` path entry, so the tests run with the real dependencies. The
  project venv and `requirements.txt` are unchanged; this was checked before and after.
- **New dependencies, spike venv only, NOT added to requirements:**
  - `cosmic-ray==8.7.0` (MIT), with `anybadge 1.16.0`, `exit_codes 1.3.0`, `parso 0.8.7`, `sqlalchemy 2.1.4`,
    `stevedore 5.9.1`, `toml 0.10.2`, `yattag 1.16.1`, `decorator 5.3.1`, `attrs 26.1.0`, `aiohttp 3.14.5`
    (plus its `aiohappyeyeballs`, `aiosignal`, `frozenlist`, `multidict`, `propcache`, `yarl`);
  - `pytest-cov 7.1.0` with `coverage 7.16.2`.
- No network calls except pip into the spike venv and the model router (Q3).

## Q1: what does validation cost? **GO**

**Collection.** `pytest --co -q`: **863 collected**, 54 deselected (the opt-in experiment tests), 917 total.

**Three consecutive full runs:**

| Run | Wall clock | Result |
|---|---|---|
| 1 | 98.2 s | 862 passed, 1 skipped |
| 2 | 101.2 s | 862 passed, 1 skipped |
| 3 | 100.8 s | 862 passed, 1 skipped |

- The pass/fail set was identical across all three runs. **No flaky test.** The one skip is
  `test_live_analyze_small_public_user`, a live-network test that skips by design.
- **One node-ID hazard:** `tests/test_safety_nonce.py::test_an_invalid_key_falls_back_to_random_never_a_constant[...]`
  is parametrized with a random hex value, so its node ID differs on every run. It always passes, but a line-to-test
  map that stores it will name a test that no longer exists. A build must pin that parameter, or map by test
  function rather than node ID.

**Per-test coverage.** `pytest --cov=backend --cov-context=test --cov-report=` took **135.8 s** for one run. It is a
one-off per repository, not per mutant.

| Measure (73 backend files, 8,601 statements) | Value |
|---|---|
| Covering tests per statement, all statements | median **3**, p90 **56**, max 147 |
| Covering tests per covered statement | median **17**, p90 **71** |
| Statements with no covering test | **3,186** |
| of which run only at import time (module-level `def`, `import`, constants: never inside a test context) | 2,559 |
| of which never executed at all | 627 |

**3-test subsets by node ID, three timings each:**

| Subset | Timings |
|---|---|
| Light (pure-Python guard tests) | 2.88 s, 2.44 s, 2.41 s |
| Heavy (needs the git-fixture scenario) | 6.81 s, 6.71 s, 6.60 s |

About 2 s of every subset is pytest start-up and imports.

**Per-mutant validation, measured on the 100 sampled mutants in Q2** (each runs its covering tests only):
- **median 7.1 s, p90 20.3 s, max 37.5 s**;
- median 11 covering tests per mutant, p90 58;
- all 100 mutants took 15.6 minutes in total.

**Finding: confirmed.** Validating a mutant costs seconds, not a full-suite run: a median of 7 s against about 100 s.
The tail (20–37 s) comes from lines covered by 50–80 tests, mostly in `backend/evidence/`. GO.

## Q2: how many valid bugs exist? **NEEDS-REDESIGN**

**Candidates.** `cosmic-ray init` only (26.8 s; nothing executed; config in `scripts/spike/cr.toml`) produced
**23,899 candidate mutations** across 182 operator variants.

| Operator family | Candidates |
|---|---|
| ReplaceBinaryOperator | 11,495 |
| ReplaceComparisonOperator | 4,841 |
| NumberReplacer | 3,468 |
| AddNot | 1,472 |
| ReplaceUnaryOperator | 679 |
| ReplaceOrWithAnd | 525 |
| ReplaceAndWithOr | 407 |
| ZeroIterationForLoop | 382 |
| ReplaceFalseWithTrue | 166 |
| ReplaceContinueWithBreak | 146 |
| ReplaceTrueWithFalse | 116 |
| ExceptionReplacer | 111 |
| RemoveDecorator | 72 |
| ReplaceBreakWithContinue | 19 |

ReplaceBinaryOperator is 48% of all candidates, mostly arithmetic to bitwise swaps (`Add_BitXor`, `Add_LShift` …).

**Filters, applied in order:**

| Filter | Surviving |
|---|---|
| (a) authored: line inside a candidate-authored range from `backend/evidence/blame.py` (`blame_file`, reused); candidate = `abhinavbansal2120@gmail.com` (hellstar-lab); the only other author is Auratech350 | **21,939** |
| (b) at least one covering test (Q1 map) | **15,156** |
| (c) not excluded (`exclusions.path_reason` and `content_reason`) | **15,115**, on 2,739 distinct lines |

**Sample.** The spec asked for 25. The first 25 (seed 5) held only 3 clean kills, too few for Q3's 5 non-telegraphing
ones, so two more independent random samples were drawn (seed 6: 25; seed 7: 50). All three are reported; none was
filtered or selected.

| Sample | Clean kill | Noisy kill | Survivor | Broken |
|---|---|---|---|---|
| seed 5 (25) | 3 | 12 | 9 | 1 |
| seed 6 (25) | 3 | 11 | 11 | 0 |
| seed 7 (50) | 14 | 15 | 20 | 1 |
| **all 100** | **20** | **38** | **40** | **2** |

- Both BROKEN results are a **cosmic-ray defect**: it treats dict unpacking `{**x}` as a power operator and emits
  `{%x}` / `{/x}`, a syntax error at collection. That's 2% of everything sampled.
- Clean kills by family: comparison 7, binary 5, AddNot 4, number 3, unary 1.

**Yield, extrapolated over the 15,115 filtered candidates** (Wilson 95% intervals):

| What counts | Rate | Estimate | 95% interval |
|---|---|---|---|
| clean kill | 20% | ≈ 3,000 | 2,000–4,400 |
| clean and non-telegraphing | 5% | ≈ 760 | 330–1,700 |
| clean, non-telegraphing and plausible | 2% | ≈ 300 | 90–1,060 |

Raw yield is far above 10 per repository, on this repository. The problem is quality, not quantity.

### Non-telegraphing: 15 of the 20 clean kills telegraph

The test: does the failing test's NAME or assertion MESSAGE effectively name the mutated symbol, or the condition,
string or exception on the mutated line or an adjacent one? This was judged by reading every case. The mechanical
check in `q2_sample.py` only matched identifiers on the mutated line, so it missed adjacent-line telegraphs such as
`path_field_is_prose`.

| Mutation | Failing test, or message | Telegraphs? |
|---|---|---|
| `structure.py:265` `/2`→`/1` | `test_fixture_matches_manifest[report_project.pdf]`: "tables: got [[2,3]]…" | no |
| `functions.py:91` `is not`→`>` | TypeError "'>' not supported between … Node" | **yes**: names the operation |
| `llm.py:312` `>= 3`→`<= 3` | `…path_field_is_prose…`, the pattern returned on the next line | **yes** |
| `verdicts/__init__.py:217` `+`→`*` | `test_document_level_cost_cap_truncates…`; the line is the cost-cap check | **yes** |
| `structure.py:162` `g + 2`→`g // 2` | manifest test `[report_two_column.pdf]`: "markers present and ascending" | no (names a feature) |
| `split.py:119` `is None`→`is not None` | `test_a_sub_bullet_is_its_own_unit…` | no (names a feature, not `list_glyph`) |
| `resume.py:355` `strong >= 3`→`<= 3` | `test_ambiguous_skill_needs_skills_context`; the line is `skills_like or strong …` | **yes** |
| `authorship.py:198` `if history`→`if not history` | message quotes the next line's string | **yes** |
| `github_client.py:71` `403`→`404` | `test_client_404_rate_limit…`: "access denied (403) … rate limited" | **yes** |
| `ensemble.py:56` `==`→`>=` | TypeError "'>=' not supported … NoneType and str" | **yes** |
| `linkcheck.py:121` AddNot on `.endswith(".git")` | "github_targets: got 'jane-doe/payments'"; the function is `github_target` | **yes** |
| `lines.py:81` `t - B`→`t + B` | `test_display_box_handles_…rotation[90…]`; the line is the `rot == 90` branch of `display_box` | **yes** |
| `github_client.py:75` AddNot on `"retry-after" in` | `…retry_after…` in the test name | **yes** |
| `pipeline.py:436` `not any`→`any` | `test_cost_cap_returns_partial_results_marked_truncated`; the line is in the cost-cap `except` | **yes** (feature-exact) |
| `lines.py:157` `ymid - centre`→`ymid % centre` | manifest test: "headings: got ['yments Platform…" | no |
| `ensemble.py:44` `!=`→`<` | `test_different_referents_disagree…`; the function is `same_resolution` | **yes** (feature-exact) |
| `structure.py:207` `/ 2`→`& 2` | manifest test: heading levels wrong | no |
| `questions/__init__.py:118` `group(1)`→`group(2)` | `test_bound_names[…]`; the function is `bound_names` | **yes** |
| `judgement.py:50` AddNot | ValueError: the message is the next line's `raise` | **yes** |
| `structure.py:227` `== 1`→`!= 1` | manifest test: "method: got set(), want {'numbering'}"; the function is `numbering_headings` | **yes** |

**Result: 15/20 (75%) telegraph.** Every one of the 5 non-telegraphing kills was killed by a coarse behaviour or
fixture test. Four are `test_fixture_matches_manifest[...]`, which compares a whole document's output with
frozen ground truth. Two of those five still hint at the feature (two-column layout, sub-bullets).

### Plausibility: would a tired human write it?

Five clean kills, diffs and an honest read of each:

```
backend/shield/structure.py:265   NumberReplacer
-        cx, cy = (d[0] + d[2]) / 2, (d[1] + d[3]) / 2
+        cx, cy = (d[0] + d[2]) / 2, (d[1] + d[3]) / 1
```
Borderline plausible: a midpoint with the wrong divisor on one half, the kind of copy-edit slip that does happen.

```
backend/claims/split.py:119   ReplaceComparisonOperator_Is_IsNot
-            if list_glyph is None:
+            if list_glyph is not None:
```
Plausible: the classic inverted guard.

```
backend/llm.py:312   ReplaceComparisonOperator_GtE_LtE
-        if is_path and len(x.split()) >= 3 and _INABILITY.search(x):
+        if is_path and len(x.split()) <= 3 and _INABILITY.search(x):
```
Plausible: an inverted threshold. But it telegraphs.

```
backend/evidence/github_client.py:71   NumberReplacer
-        if resp.status_code in (403, 429) and (
+        if resp.status_code in ( 404, 429) and (
```
Very plausible: a wrong status code is a real-world bug. But it telegraphs.

```
backend/shield/structure.py:207   ReplaceBinaryOperator_Div_BitAnd
-        size = round(ln.size * 2) / 2
+        size = round(ln.size * 2) & 2
```
Machine noise: no human writes `& 2` for "round to the half point".

Across all 20 clean kills, about **12 read as plausible**:
- comparison boundary and equality flips;
- `is`/`is not`;
- AddNot / Delete_Not;
- NumberReplacer on status codes and indices;
- one sign flip (`- B`→`+ B`).

About **8 are machine noise**:
- arithmetic to bitwise or modulo (`& 2`, `% centre`, `// 2`, `cost *`);
- comparison swaps that only raise a TypeError (`is not`→`>`, `==`→`>=`, `!=`→`<`);
- a doubled negation.

Plausible and non-telegraphing together: **2 of 100 sampled** (`structure.py:265`, `split.py:119`).

### Why NEEDS-REDESIGN, and what would change (proposed, not built)

The quantity is there; the hunt is not. When three quarters of clean kills name the mutated function or condition
in the failing test's name or message, finding the bug is reading, not understanding. Proposed changes:

1. **An operator allow-list of human-plausible mutations.** Comparison boundary flips (`<`↔`<=`, `>`↔`>=`), `==`↔`!=`,
   `is`↔`is not`, AddNot / Delete_Not, `and`↔`or`, NumberReplacer restricted to ±1 and to literal constants.
   - Drop arithmetic-to-bitwise, arithmetic-to-modulo or floordiv, and comparison swaps that change types.
   - Drop `**` dict-unpacking candidates (the cosmic-ray defect).
2. **A telegraph filter, before validation.** Reject a kill when the failing test's name or message contains the
   enclosing function's name, an identifier or string literal from lines ±3, or the mutated operator (TypeError
   text).
3. **Prefer kills by behaviour-level tests.** All five non-telegraphing kills came from fixture or behaviour tests,
   so a kill by a unit test named after its function is, on this evidence, almost always telegraphed. That needs a
   test-level signal; the simplest is the test name not mentioning the enclosing function.
4. **Deduplicate per line, and pin node IDs** (the random-parameter test from Q1).

On Crucible these would leave on the order of 90–1,000 usable bugs. A real candidate repository with a tenth of the
tests could fall under 10. Measure on two or three real candidate repositories before building.

## Q3: is the AI stranger baseline fair? **NEEDS-REDESIGN**

**Inputs.** The 5 clean, non-telegraphing kills from Q2: `structure.py:265`, `structure.py:162`, `split.py:119`,
`lines.py:157` and `structure.py:207`. That is all five that exist in the 100-mutant sample, so this is not a
selection. Four are in the PDF layout code.

**Router.** The existing model router (`stranger_agent` task), temperature 0, thinking off, nano and super.

**Conditions:**
- **A, file-level:** the mutated file with line numbers, plus the failing test name and assertion message. The
  model names the line.
- **B, repo-level:** the test name and assertion, plus the repository's Python file tree (`backend/`, `tests/`,
  `scripts/`). The model asks for up to 5 files, gets their contents (with the mutated file mutated), then names
  path and line.

Up to 3 attempts per condition. A miss is fed back ("already ruled out: X"). A hit is the exact mutated line. One
reply of the 84 model calls (54 attempts; $0.084 in total) was unparseable and is counted as a miss. Script: `scripts/spike/q3_stranger.py`.

| Condition | Tier | Hit, first attempt | Hit, within 3 | Seconds per attempt (median, range) | Cost per attempt (median) |
|---|---|---|---|---|---|
| A (file) | nano | 0/5 | 0/5 | 1.5 (0.7–3.1) | $0.00072 |
| A (file) | super | **3/5** | 3/5 | 1.8 (1.1–6.2) | $0.0036 |
| B (repo) | nano | 0/5 | 0/5 | 2.1 (1.3–15.8) | $0.00077 |
| B (repo) | super | **0/5** | 0/5 | 1.6 (1.2–2.6) | $0.0026 |

Notes:
- **Time to a hit:** super in condition A hit in 1.8 s, 1.4 s and 1.8 s, every time on the first attempt.
- **Which file condition B opened:** super picked the mutated file 1/5 times, nano 2/5. Super's misses in B were
  mostly `structure.py:112–114` and `evaluate.py:112–114`, a near-identical "line" in the wrong place.
- **A near miss:** super in B opened `split.py` and answered line 120 three times. The bug is on line 119; 120 is
  the assignment inside that `if`. That is a miss under the exact-line rule.
- **Harness weakness:** models often repeated an answer already ruled out (nano A: 111, 111, 111), so the
  within-3 rates are lower bounds.
- n = 5 per cell, so every rate has an interval of roughly ±40 points. These are directions, not estimates.

**The consequence, plainly:**

1. **Condition A is not near 100%, but it is not a fair baseline either.** Super, handed the file, finds a
   non-telegraphing bug 3 times in 5, in under 2 seconds. If the human hunts the whole repository while the model is
   handed the file, that is a handicap, not a baseline, and a judge will say so. If the baseline is A, the human
   must be given the same single file.
2. **Condition B, as built, is a blindfolded stranger.** The model cannot grep, cannot read the failing test's
   source, cannot run the test, and sees the code only through a 5-file pick. 0/5 here mostly measures that
   handicap. A judge would say the AI lost because it was blindfolded.
   - A fair condition B gives the AI the same affordances the human has: the checkout, the test source, the ability
     to run the failing test, search, and a time or attempt budget. It should be measured as an agent loop.
   - That is the redesign this question needs. Its result is unknown, and given condition A it could well be much
     higher than 0/5.
3. **Speed is not the story.** The model answers in 1–2 seconds per attempt, and any human hunt takes minutes.
   "The candidate beat the AI" on time will never be true. The numbers support only an **author-versus-stranger**
   framing:
   - Given identical information (the same files, the same failing test, the same tools), does the person who wrote
     the code localise the bug more often, within a budget, than a capable stranger? The AI stands in for that
     stranger.
   - The comparison is accuracy within a fixed budget, not time, and the information the two sides get must be
     declared and identical.

**Verdict.** NEEDS-REDESIGN. Neither condition is a fair baseline as built:
- Condition A is too strong unless the human gets only the file.
- Condition B is too weak to be credible.

Redefine the baseline as a tool-using stranger with the human's exact affordances, then re-measure, before any
"author vs AI" claim.

## Raw artefacts (session scratchpad; not committed)

- Q1:
  - `q1/run{1,2,3}.xml|log`: full runs;
  - `q1/.coverage`: per-test contexts;
  - `q1/line_map.json`: line to covering tests, 73 backend files.
- Q2:
  - `q2/session.sqlite`: cosmic-ray init;
  - `q2/candidates.json`, `q2/survivors.json`: 15,115 candidates;
  - `q2/sample{,2,3}.json|log`: each mutant's diff, failing tests, messages, timings;
  - `q2/all.json`.
- Q3: `q3.json` and `q3.log`.

## Tree state

- Every applied mutation was reverted with `git checkout`, and `git diff --quiet` was asserted after each of the 100
  mutants and after each Q3 read.
- Final `git status --short` shows no tracked file modified. Untracked: only `scripts/spike/` (throwaway) and this
  document.

---

# Phase 5, round 2: Q2′ / Q3′ re-measure, then the gated build (2026-10-11)

| Part | Result |
|---|---|
| Part 1, Q2′ | **Gate passes** on Crucible: about 630 clean, non-telegraphing, plausible candidates (95% interval 270–1,370) against a gate of 15. There is a concentration risk. |
| Part 2 | **Built:** the `bugpool/` offline pool generator plus the session refusal path. Crucible's pool holds 15 validated candidates. |
| Part 3, Q3′ | **Parity harness built.** A tool-using stranger (Super, 15 tool calls) found the planted line in **2 of 10**. Files opened is the discriminator. |

`backend/` is untouched (0 diff lines). Every mutation was reverted, and the real working tree was never mutated: the
pool generator and the agent work in throwaway git worktrees. Everything ran in the spike venv only.

## Part 1, Q2′: re-measure with behaviour tests and an operator whitelist

### 1. What each test touches

This comes from the coverage-context DB. Tests are keyed `path::function`, so parametrized ids collapse:
**414 test functions**.

| Measure | min | median | p75 | p90 | max |
|---|---|---|---|---|---|
| distinct `backend/` modules touched | 1 | 3.5 | 12 | 18 | 31 |
| statements covered | 1 | 146 | 399 | 801 | 1,687 |

Histogram of modules touched:

| Modules | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Tests | 154 | 34 | 19 | 6 | 5 | 6 | 28 | 9 | 7 | 15 | 20 | 14 | 3 | 8 | 17 | 17 | 9 | 26 | 17 |

The distribution is bimodal. The chord ("kneedle") knee of the sorted curve falls at **3**, which would select 226
tests, but that point is inside the unit-test mass. The tests at 3 are `test_reference_kind` and
`test_kind_and_route`, which are function-level.

**Threshold chosen: 7 or more modules = behaviour test. It selects 190 of 414 tests.** That is the edge of the second
population, after the 4–6 valley. Examples: `test_2pt_black_injection_is_caught`,
`test_scan_pdf_makes_zero_model_calls_on_every_fixture`, `test_fixture_matches_manifest`. This departs deliberately
from the literal knee, which sits inside the first mode.

### 2. Restricted candidates

**Whitelist** (cosmic-ray names): comparison swaps within `== != < <= > >=`, `Is`↔`IsNot`, `True`↔`False`, `AddNot`,
`UnaryOperator_Delete_Not`, `And`↔`Or`, `Break`↔`Continue`, `NumberReplacer`. cosmic-ray's NumberReplacer applies
exactly +1 and −1 (its `OFFSETS`), so it already satisfies the {−1, 0, +1} deltas; a 0 delta is not a mutation.
Cross-family comparisons such as `Is`→`Gt` are excluded, because they only raise TypeError.

| Step | Candidates |
|---|---|
| raw | 23,899 |
| whitelist | 10,261 |
| authored (`blame.py`) | 9,461 |
| not excluded | 9,420 |
| **covered by at least one behaviour test** | **7,565**, on 2,181 lines in 59 files |

By family: comparison swap 2,650 · number ±1 2,262 · not inserted 1,199 · and/or 747 · is/is not 267 ·
not removed 226 · boolean 137 · break/continue 77.

### 3. Sample of 60 (seed 11), each run against its covering tests only

| Outcome | Count |
|---|---|
| survivor | 32 |
| noisy kill (2+ test functions fail) | 16 |
| **clean** | **6** |
| telegraphed | 6 |
| exception at the mutated line | 0 |
| broken | 0 |

The whitelist removed both the TypeError swaps and the cosmic-ray `**` defect: no exception-at-site, no broken.

All six "telegraphed" calls hold up on reading:
- `401`→`400` dies with "GitHub API 401 …";
- `n <= 1`→`n < 1` dies on `'one commit'`;
- `group(1)`→`group(2)` is killed by `test_bound_names`.

### 4. Yield, and an honest read of the diffs

Six clean kills came from the 60. Each, with a verdict on whether a tired human would write it:

```
pdf_security.py:219   comparison swap
-        if seen > MAX_ATTACHMENTS or not isinstance(fs, DictionaryObject):
+        if seen <= MAX_ATTACHMENTS or not isinstance(fs, DictionaryObject):
```
Plausible: an inverted cap. Killed by `test_fixture_matches_manifest` with a downstream `IndexError: list index out of
range` raised in the eval harness, which does not point at the code.

```
structure.py:507   break <-> continue
-            continue
+            break
```
Plausible. Message: "text-only link: got []". That names the *links* feature, not `build_links`.

```
structure.py:245   comparison swap
-    if len(widths) != 1 or next(iter(widths)) < config.TABLE_MIN_COLS:
+    if len(widths) == 1 or next(iter(widths)) < config.TABLE_MIN_COLS:
```
Plausible. Message: "tables: got []", which names a feature.

```
lines.py:157   comparison swap
-        if clusters and abs(ymid - centre) <= LINE_TOLERANCE_EM * max(size, sz):
+        if clusters and abs(ymid - centre) > LINE_TOLERANCE_EM * max(size, sz):
```
Plausible. Downstream IndexError.

```
lines.py:180   not inserted
-                if gap > max(SEGMENT_GAP_EM * med, SEGMENT_GAP_MIN_PT):
+                if not gap > max(SEGMENT_GAP_EM * med, SEGMENT_GAP_MIN_PT):
```
Plausible in meaning, unnatural in form: a human writes `<=`, not `not … >`.

```
lines.py:134   number +/-1
-            ... < max(c.font_size, 1) * 0.8
+            ... < max(c.font_size, 1) * -0.19999999999999996
```
**Machine noise.** NumberReplacer on a float leaves floating-point garbage. The pool generator now excludes floats.

Only 6 clean kills exist in the sample, so 8 diffs are not available; all 6 are shown.

**Yield: 5 of 60 are clean, non-telegraphing and plausible (8.3%). Over the 7,565 restricted candidates that is
≈ 630, with a Wilson 95% interval of 270–1,370.** Two of the five hint at a feature ("links", "tables"). Counting
only the three with no hint at all: 3/60 ≈ 380 (130–1,040).

**GATE: PASS** (≥ 15 per repo), on Crucible.

**Caveat, measured:** every one of the 5 is in the PDF layout code (`backend/shield/`), and all 5 are killed by the
**same test function**, `test_fixture_matches_manifest`. A golden-fixture test that checks a whole document's output
is what makes non-telegraphing kills possible here. A candidate repository without that kind of test may not pass
this gate.

## Part 2: the pool generator (`bugpool/`)

`bugpool/` is a top-level package, not under `backend/`. It imports `backend.evidence` read-only (blame, counting,
exclusions). It needs cosmic-ray and pytest-cov, so it runs in the spike venv. Its tests are in `bugpool/tests/`,
outside the main suite's `testpaths`, and skip when cosmic-ray is absent.

```
<spike-venv>/bin/python -m bugpool generate --repo . --email abhinavbansal2120@gmail.com --out pool.json --target 15 --max-validations 250
```

It is an **offline batch job**, run in a temporary `git worktree` at HEAD, so the real tree is never touched. In order:
1. Build the coverage map.
2. Enumerate candidates with `cosmic-ray init` (nothing executed).
3. Filter: whitelist; integer literals only for ±1; authored (`blame.py`); not excluded; behaviour-covered.
4. Validate one candidate per line, in seeded order, until the target is reached or the validation cap hit.
5. Write the pool file.

Each pool candidate carries:
- the file and line (and end line), the diff, and the original and mutated lines;
- the operator and its family;
- the **killing test** (`path::function`) and its message;
- the covering tests and the behaviour tests;
- the checks it passed: `clean_kill`, `exception_at_site: false`, `telegraph_hits: []`, and plausibility (a
  whitelisted family; integer literals only).

**Refusal path.** If the repository has no tests, no behaviour test, no authored line covered by one, or no candidate
validates, the pool is `status: "empty"` with the reason recorded. `bugpool.session.plant()` then returns
`{"planted": false, "message": "insufficient coverage to plant a validated bug in authored code", ...}` and plants
nothing. Tested on a fixture repo with no tests: the pool is empty, plant refuses, and the tree and worktree list are
unchanged. Also tested on a fixture with only one-module unit tests: empty, with reason "no behaviour test".

**Second tier.** The characterization-test fallback is **not built**. It is recorded in every pool
(`"second_tier": "planned second tier, not built: ..."`). The seam is `generate_pool(second_tier=callable)`: the
callable is invoked only when the pool would otherwise be empty.

**Q1 fix: line → test is stored as `path::function`, never a parametrized node id.**
- On Crucible, the map was built three consecutive times in a worktree. All three were identical: 863 tests, 6,136
  lines mapped, 414 test keys.
- That check also exposed one more defect. `tests/test_claims_skills.py::test_split_items[Python; Go | Rust • C++…]`
  has a `|` inside its parameter id, so splitting the coverage context on the first `|` left `test_split_items[Python;
  Go `. The map was still stable, because that parameter is deterministic, but the key was not a runnable selector.
- **Fixed:** `test_key` now strips the phase suffix from the right and drops everything from the first `[` of the last
  segment. A regression test pins it.
- The Crucible pool below was generated before the fix. Its only exposure was a `skills.py` candidate covered by that
  test, which would have been classified broken. None is in the pool.

**Crucible's pool**, generated before the fix:
- `status: ok`, **15 candidates**;
- 164 validations: 15 clean, 73 survivor, 53 noisy, 22 telegraphed, 1 exception at site;
- 2,266 s (37.8 min), including the coverage run;
- killing tests: `test_fixture_matches_manifest` × **11**, the report-page fixture test × 2, a retrieval test × 1, a
  target test × 1;
- files: `shield/lines.py` 4, `structure.py` 2, `fonts.py` 2, `grounding/html.py` 2, and 1 each in `docx_scan.py`,
  `doc_kind.py`, `pdf_security.py`, `retrieval/rules.py` and `evidence/question_surface.py`.

Plausibility of the 15, read honestly:
- **About 12 read as a tired human's bug:** inverted `is`/`is not` guards, flipped bounds, wrong tuple indices
  (`prev[1][2]`→`prev[1][3]`, `prev[2]`→`prev[1]`).
- **2 are machine noise:** `runs[-1].bold == bold`→`>`, and `>`→`!=` on a float gap.
- **2 are right in meaning but unnatural in spelling:** `not x is None`. A human writes `x is not None`, so the odd
  spelling itself could give the line away.

The automated checks cannot see that last point. Many manifest-test messages also still name a *feature*
("tables: got …", "has_form: got False"), though never the symbol.

## Part 3, Q3′: the parity harness (`scripts/spike/q3b_agent.py`)

The AI Stranger is now a **tool-using agent with exactly a human's affordances**:
- `list_files`, `read_file` (250 numbered lines per call), `run_tests` (any pytest selector, or the whole suite), and
  `answer`;
- the failure output comes back from `run_tests`;
- nothing else: no search tool and no traceback beyond what pytest prints.

The rest of the setup:
- **Budget:** 15 tool calls, then a forced answer. Super only, through the existing router (task `stranger_agent`).
- **Isolation:** each candidate gets a throwaway worktree at the pool's commit, with the bug planted by
  `bugpool.session.plant`. The tools refuse dotfiles, `.env*` and anything outside the worktree.
- **Hit rule:** the answer must name the mutated file and a line within the mutated span.

| Candidate | Found | Files opened | Tool calls | Cost | Wall clock* |
|---|---|---|---|---|---|
| `shield/docx_scan.py:266` | no | 2 | 15 (forced) | $0.0149 | 127 s |
| `shield/structure.py:255` | **yes** | 3 | 13 | $0.0278 | 126 s |
| `shield/lines.py:137` | no | 3 | 15 (forced) | $0.0164 | 132 s |
| `shield/lines.py:205` | no | 3 | 15 (forced) | $0.0158 | 132 s |
| `grounding/html.py:329` | no | 1 | 15 (forced) | $0.0125 | 123 s |
| `grounding/html.py:246` | no | 3 | 15 (forced) | $0.0215 | 124 s |
| `shield/doc_kind.py:21` | **yes** | 7 | 12 | $0.0264 | 124 s |
| `retrieval/rules.py:171` | no | 4 | 15 (forced) | $0.0232 | 127 s |
| `shield/lines.py:179` | no | 2 | 7 | $0.0081 | 114 s |
| `shield/structure.py:242` | no | 2 | 15 (forced) | $0.0135 | 133 s |

*Wall clock is recorded, never compared. Most of it is the agent's single full-suite run.

**Headline: the stranger found the planted line in 2 of 10 within a 15-call budget.**

The discriminator is files opened. The two hits opened 3 and 7 files; across all ten, the agent opened the mutated
file in only **3 of 10** runs and answered correctly in 2 of those 3. Cost was $0.18 in total, a median of $0.016
per candidate.

What the agent did with its budget (measured):
- it ran the tests exactly once in every run;
- it spent 9–13 of its 15 calls on `list_files` in six runs;
- 7 of 10 runs hit the budget and gave a forced answer with no line.

An earlier run of the same harness, before the refusal false positive below was handled, found **1 of 8** (log
kept), so **3 of 18** across both runs.

**What this does and does not show.**
- **This is a weak stranger.** It is a plain JSON tool loop with no search tool, a small read window and 15 calls.
  A production coding agent with search, a larger budget, or a better scaffold would very likely do much better.
  The 20% is a lower bound for "an AI stranger", not a measurement of the best one.
- **The comparison is not run yet.** The human side of the same harness (same tools, same budget, same failure
  output) has not been measured.
- **Speed is not compared.** It is not the story, and per Q3 never will be.
- **A false positive in our own router:** in the first run, its lexical refusal detector fired on the agent's own
  free-text `thought` field and crashed the run. It is now recorded as a turn with no tool call; it did not recur in
  the second run.

## What the author-versus-stranger measurement does and does not claim

This measurement asks one thing: given the same repository, the same planted bug, the same tools and the same
budget, does the person whose identity the code is committed under find the bug by opening fewer files, more often,
than a capable stranger? It claims nothing about who typed the code. Blame attribution means "committed under this
identity", not "hand-written": code produced with an AI assistant, pair-programmed, or copied in and committed by the
candidate counts as theirs. On Crucible itself most of the code was written in AI-assisted sessions and committed
under `hellstar-lab`. What is measured is the ability to navigate and explain code committed under one's name, which
is what an interviewer actually relies on when a candidate says "I built this". It is not a test of authorship in the
typing sense, not a speed contest, and not a verdict on the candidate. Its result is a fact for the human reviewer,
with the same caveats as every other row in the report.

---

# Phase 5, round 3: stranger parity, diversity, the timed session (2026-10-12)

## Part D: the refusal detector (`backend/llm.py`)

**Cause.** A tool-using agent on Super wrote a 357-token `thought` containing "I cannot …" while reasoning aloud. The
lexical refusal detector matched `first_person_cannot` and raised `LLMRefusalError`, which crashed the first Q3′ run
(logged as `refusal_where="thought:first_person_cannot"`).

**Fix, at the detector:**
- In a reasoning field (`thought`, `reason`, `rationale`, `evidence_summary`, …), or in any string longer than 240
  characters, only refusal-shaped language counts:
  - a first-person refusal of the request ("I cannot help / assist / comply / fulfill / provide / answer …");
  - an opening apology;
  - "as an AI";
  - "against my guidelines".
- Short answer fields keep the full set, and every earlier refusal test still passes.
- The in-memory exception now carries the matched excerpt; the call log stays content-free.

**Tests** (`tests/test_llm.py`):
- **Regression:** long agent reasoning is not a refusal.
- **Thinking aloud:** reasoning fields and long text don't trip the detector.
- **Real refusals:** a real refusal inside reasoning is still caught.
- **Excerpt placement:** the excerpt is on the exception and absent from the log.

The regression uses a labelled **reconstruction**, not the verbatim text: the log stores prompt hashes only, by design,
so the crashing text was never kept. FEEDBACK.md #14 records this.

The workaround is gone from `scripts/spike/q3b_agent.py`.

## Part A: stranger parity

The stranger now has **exactly the participant's view and actions**, through the shared `bugpool/tools.py`:
- **View:** it starts with the repository's file tree and the failing test's name and output.
- **Actions:** `FILE_OPEN`, `SEARCH` (regex over tracked, non-hidden files), `TEST_RUN`, `ANSWER`.
- **Sandbox:** the same worktree sandbox; `.env` and dotfiles are blocked.
- **Budget:** a ceiling of 40 actions. Super only.

The same 10 candidates as round 2 (`scripts/spike/q3c_agent.py`).

**Found-rate as a function of budget** (found within b actions, counting the ANSWER):

| Budget b | 1–24 | 25 | 30 | 35 | 40 |
|---|---|---|---|---|---|
| Found | 0/10 | 1/10 | 1/10 | 1/10 | **1/10** |

A run that answered correctly at action k counts as found for every b ≥ k. A run capped lower would have been forced
to answer earlier, so the curve is an upper envelope for smaller budgets.

| Candidate | Found at action | Files opened | Actions used | Cost | Wall clock* | Submitted |
|---|---|---|---|---|---|---|
| `shield/docx_scan.py:266` | — | 5 | 41 (forced) | $0.412 | 119 s | `MANIFEST.json`, no line |
| `shield/structure.py:255` | — | 5 | 41 (forced) | $0.375 | 268 s | `structure.py:367` |
| `shield/lines.py:137` | — | 3 | 41 (forced) | $0.417 | 142 s | `linkcheck.py:34` |
| `shield/lines.py:205` | — | 5 | 41 (forced) | $0.375 | 193 s | `config.py:69` |
| `grounding/html.py:329` | — | 4 | 41 (forced) | $0.345 | 95 s | `html.py:328` (one line off) |
| `grounding/html.py:246` | — | 4 | 41 (forced) | $0.373 | 117 s | `html.py:416` |
| `shield/doc_kind.py:21` | **25** | 6 | 25 | $0.121 | 68 s | `doc_kind.py:21` ✓ |
| `retrieval/rules.py:171` | — | 5 | 41 (forced) | $0.238 | 88 s | a yaml fixture, no line |
| `shield/lines.py:179` | — | 4 | 19 | $0.099 | 68 s | `fonts.py:17` |
| `shield/structure.py:242` | — | 5 | 32 | $0.240 | 129 s | `structure.py:255` |

*Recorded, never compared.

**Round 2 against round 3, same 10 candidates:**

| | Round 2 | Round 3 |
|---|---|---|
| Starting view | had to run the tests to see the failure | the failing test and its output, up front |
| Tools | `list_files` / `read_file` / `run_tests`; no search | `FILE_OPEN` / `SEARCH` / `TEST_RUN`, the participant's set |
| Budget | 15 tool calls | 40 actions |
| **Found** | **2/10** (at 13 and 12 calls) | **1/10** (at 25) |
| Opened the mutated file | 3/10 | **7/10** |
| Cost | $0.18 in total | **$3.00 in total** (median $0.36) |

**Read honestly:**
- More information, search and almost three times the budget did not raise the found-rate. The stranger now reaches
  the right file 7 times in 10 (against 3), but it then pages through it: `FILE_OPEN` is 35–40 of its 40 actions,
  `SEARCH` 0–5.
- It never localises the line in a large file. Two answers landed in the right file one line or one candidate away
  (`html.py:328` for 329; `structure.py:255`, another pooled line, for 242), and exact-line scoring counts both as
  misses.
- **This is still a weak scaffold.** It doesn't know to search for the failing assertion's text, or to bisect. The 1/10
  measures *this* stranger.
- **Files opened is the discriminator, but it is near-flat:** 3–6 files for every run, hit or miss. The difference
  between strangers would show up in how they read those files, and actions alone capture that only partly.
- Round 3's view is the one the participant gets, so its numbers, not round 2's, are the ones a session record
  compares against.

## Part C: the timed session (`bugpool/session.py`, `python -m bugpool session`)

**Lifecycle:**
1. An empty pool is refused with its recorded reason; nothing is planted.
2. One candidate is chosen and planted in a throwaway git worktree. The participant's real tree is never modified.
3. The participant sees the file tree, the failing test's name and its output, and nothing else: no diff, no hint, no
   module name. A test asserts that neither the diff nor the mutated line appears in that view.
4. Every action is logged with a timestamp and a type: `FILE_OPEN`, `SEARCH`, `TEST_RUN`, `ANSWER`. These are
   `bugpool.tools.ACTIONS`, the stranger's exact set, through the same implementation.
5. One `ANSWER` (file and line) ends the hunt. The outcome records correct or incorrect, actions used, distinct files
   opened and elapsed seconds. Running out of the action budget is recorded as `not_found_within_budget`, a valid
   outcome, not a failure.
6. The worktree is destroyed and the session record written.

**The session record** holds:
- the candidate's id, operator, family and killing test;
- the full action log;
- the outcome;
- a `comparison` block: the participant's actions-to-find, and the **stranger's budget curve for the same candidate**,
  read from the stranger's results by candidate id.

There is no score, no rank and no hire field; a test scans every key of the record for them. Elapsed time is recorded,
never the headline, and never compared.

**UI.** A plain terminal REPL, the simplest surface to demo: `tree`, `open PATH [START]`, `search PATTERN [PATH]`,
`test [SELECTOR]`, `answer PATH LINE`, `quit`. `tree` is free (not an action), because the stranger is given the tree
in its brief.

**Tests** (`bugpool/tests/test_session.py`, offline, a fixture repo):
- the empty-pool refusal (no worktree is created);
- the view and the sandbox: dotfiles can't be listed, opened or searched, and `../` paths are refused;
- the action log and a correct answer, with the stranger curve attached;
- budget exhaustion as an outcome.

The worktree is gone after every test, and the real tree is unchanged.

## Part B: pool diversity and the restated gate

**Added to `generate_pool`:**
- **Diversity:** at most 2 candidates per killing test (`max_per_killing_test`) and at most 3 per module
  (`max_per_module`).
  - A module that is full is skipped before validation.
  - A clean kill whose killing test is already at its cap is dropped and counted.
- **Ruff lint gate:** a mutation is rejected when it introduces a ruff diagnostic on its line(s) that the original
  lines did not have. The selection is explicit, `E7,E9,F,SIM,W6`: ruff 0.17's default selection reports nothing for
  `not x is None`. It catches E714 (`not x is None`), E713 (`not x in`), E712 (`== True`) and SIM201 (`not a == b`). It
  runs before any test.
  - New dependency, spike venv only: `ruff 0.17.0`, not in requirements.
- **Number replacement:** integer literals only, constants and subscript indices alike (`d[0]`, `prev[2]`). Never a
  float, never a complex literal. Every candidate records `lint_clean` and `integer_literal_only` among its checks, and
  carries an `id`.

**Crucible's pool, regenerated under the constraint** (target 15, validation cap 400, seed 0, 95 min):

| | Value |
|---|---|
| **Candidates** | **9** (stopped at the 400-validation cap) |
| Validated | 400: 183 survivor, 132 noisy, 49 telegraphed, **20 clean**, 15 lint-rejected, 1 broken |
| Dropped for diversity | **11 of the 20 clean kills**, every one because `test_fixture_matches_manifest` already killed 2 |
| **Killing tests** | **8**: `test_fixture_matches_manifest` × 2; `test_every_claim_traces_to_its_unit_span`, `test_fixture_matches_ground_truth_exactly`, `test_bug_targets_and_hard_anchor_rule`, `test_resolver_matrix`, `test_statuses_never_kill_the_batch`, `test_ocr_text_is_added_when_available`, `test_questions_render_traced_and_neutral` × 1 each |
| **Modules** | **9**, one candidate each: `claims/split.py`, `shield/fonts.py`, `shield/pdf_security.py`, `evidence/blame.py`, `evidence/question_surface.py`, `evidence/resolve.py`, `evidence/target.py`, `shield/analyze.py`, `grounding/html.py` |

### The restated gate: **Crucible does NOT pass**

| Criterion | Required | Crucible | Result |
|---|---|---|---|
| Candidates | 15 | **9** | **fail** |
| Killing tests | 5+ | 8 | pass |
| Modules | 3+ | 9 | pass |

The gate is not loosened.

**The blocker is the same concentration measured in round 2, now made binding.** Without the diversity cap, 13 of the
20 clean kills come from one golden-fixture test. With it, clean kills arrive at about 9 per 400 validations, roughly
2.25%. At that rate 15 would need on the order of 670 validations, about 2.6 hours on this machine.

Raising the validation cap would only search longer. It might reach 15, but this round did not run it, and it does not
change the finding: outside one test, Crucible's suite kills a non-telegraphing, lint-clean, plausible mutation rarely.

### Plausibility of the 9, read honestly

- **Plausible:**
  - `fonts.py:44` (`>`→`<=`);
  - `pdf_security.py:123` (`< 4`→`>= 4`);
  - `target.py:187` (`continue`→`break`);
  - `analyze.py:165` (`if ocr_done`→`if not ocr_done`);
  - `html.py:45` (`>=`→`<=`).
- **Semi-plausible:** `question_surface.py:149` (`<`→`!=`); `split.py:178` (`sec_index += 1`→`+= 0`). A human forgets
  an increment; they rarely write `+= 0`.
- **A form fingerprint:** `blame.py:120` (`ranges[-1]`→`ranges[- 0]`) and `resolve.py:171` (`candidates[0]`→
  `candidates[ -1]`).
  - cosmic-ray's `NumberReplacer` writes `" " + str(val)` (its source), so every number mutation leaves a stray space:
    `[ -1]`, `[- 0]`, `( 404, 429)`. A hunter scanning for odd spacing finds it without understanding the code.
  - Neither the lint gate nor the telegraph check catches it.
  - **Recorded, not fixed:** the pool generator should normalise the mutated literal's spacing, or reject number
    mutations whose rendered form differs from the file's style.

## Status

| Part | State |
|---|---|
| Part A | Done: the stranger with search and a 40-action ceiling, the budget curve, the per-candidate table, round 2 and 3 compared. **1/10 found** |
| Part B | Done: diversity and lint gate in; Crucible **fails the restated gate on size (9 < 15)** |
| Part C | Built and tested; ready to demo |
| Part D | Fixed and tested |

Not built this round: the characterization / record-replay oracle (it needs its own spec), and the cosmic-ray spacing
fingerprint fix.

---

# Phase 5, round 4: the format gate, scoring, the session report (2026-10-12)

## Part A: the format gate (`bugpool/fingerprint.py`)

**Generator (`format_gate`).** Format the file with ruff, apply the same mutation (operator and occurrence) to the
formatted copy, then format again. If the second format changes anything, the mutation introduced non-canonical form
and the candidate is rejected (validation class `format`). Formatting preserves the order of AST nodes, so occurrence
N in the original is occurrence N in the formatted file. The gate runs after the lint gate and before any test.

**Plant time (`check_diff`, called by `plant()`).** It requires:
- exactly one changed line;
- no whitespace-only hunk;
- token spacing identical outside the changed tokens.

Otherwise it raises `PlantError` and plants nothing, and `Session.start` destroys its worktree before re-raising. The
token-spacing rule is what catches `candidates[0]`→`candidates[ -1]`: that hunk is not whitespace-only, so the literal
rule alone would pass it.
- Tested with that exact case as the negative fixture: `test_plant_refuses_a_spacing_fingerprint_loudly`.
- Also tested: `test_check_diff` (5 cases) and `test_format_gate_rejects_the_cosmic_ray_spacing_and_passes_a_clean_swap`.

**Re-screen of the existing 9-candidate pool: 7 of 9 survive. 2 FAIL, and they are bugs that would have been findable
without reading the code:**

| Pooled line | Mutated to | Format gate | Plant check |
|---|---|---|---|
| `backend/evidence/blame.py:120` `start, end = ranges[-1]` | `ranges[- 0]` | formatter rewrites it to `ranges[-0]` | spacing before `0` changed |
| `backend/evidence/resolve.py:171` `ref, scoped = candidates[0]` | `candidates[ -1]` | formatter rewrites it to `candidates[-1]` | spacing before `-` changed |

Round 3's ten stranger candidates (the older pool) contain **three more** such fingerprints:
- `lines.py:137` (`prev[1][ 3]`);
- `lines.py:205` (`d[ -1]`);
- `lines.py:179` (`prev[ 1]`).

A real session now refuses all three. They were planted in round 4 only through an explicit, measurement-only
`form_check=False`, to keep the ten comparable, and are marked below. The stranger never even opened the file for any
of the three.

## Part B: scoring and the localisation metric

**Statement granularity (`tools.statement_span`).** The answered line is mapped through the AST to its innermost
statement, and any line of the mutated statement is a hit. A compound statement's header (`if a < b and (…):`) is its
own span, so a line inside the if-body is not a hit for a mutation in the condition.

| | Exact line | Statement |
|---|---|---|
| Round 3 (re-scored from its saved answers) | 1/10 | **1/10** |
| Round 4 (re-run, same 10 candidates) | 2/10 | **2/10** |

Statement granularity changed nothing here. The near misses (`html.py:328` for 329; `rules.py:174` for 171) are
separate statements.

**Localisation cost** is the number of actions between first opening the correct file and answering correctly; a run
that never localised the bug is `unbounded`. Round 3 did not keep per-action paths, so these numbers are from a
**re-run (round 4)** of the same ten candidates with full action logs. Super, budget 40, the same four actions,
$3.09 in total.

| Candidate | Fingerprinted* | Found | Localisation cost | First opened the file at action | Files opened | Actions used |
|---|---|---|---|---|---|---|
| `shield/docx_scan.py:266` | | no | **unbounded** | 8 | 5 | 41 (forced) |
| `shield/structure.py:255` | | **yes** | **37** | 4 | 4 | 41 (forced, correct) |
| `shield/lines.py:137` | yes | no | **unbounded** | never | 3 | 41 (forced) |
| `shield/lines.py:205` | yes | no | **unbounded** | never | 6 | 41 (forced) |
| `grounding/html.py:329` | | no | **unbounded** | 4 | 4 | 41 (answered 328) |
| `grounding/html.py:246` | | no | **unbounded** | 3 | 4 | 41 (forced) |
| `shield/doc_kind.py:21` | | **yes** | **13** | 4 | 5 | 17 |
| `retrieval/rules.py:171` | | no | **unbounded** | 2 | 4 | 26 (answered 174) |
| `shield/lines.py:179` | yes | no | **unbounded** | never | 7 | 41 (forced) |
| `shield/structure.py:242` | | no | **unbounded** | 11 | 4 | 27 (answered 367) |

*Would be refused at plant time under round-4 rules; planted here only through the measurement-only bypass.

**Found-rate by budget** (round 4; the budget counts actions before the answer):

| Budget b | 1–15 | 16–39 | 40 |
|---|---|---|---|
| Found | 0/10 | 1/10 | **2/10** |

Two ways to read localisation cost:
- **It separates where files-opened did not.** Both finds opened 4–5 files, as did most misses. What differs is that
  the stranger opened the right file early in 7 of 10 runs (actions 2–11) and then could not localise the line within
  the budget: 13 actions in one find, 37 in the other, unbounded in every miss.
- **In the session record it is now the headline**, set beside the stranger's on the same candidate. Files opened is a
  recorded field under the outcome. Found-rate-by-budget stays in the stranger's curve.

**Is a pooled line's unmutated form distinguishable by a cheap signal?** Measured on all 24 pooled lines of both pools
(`scripts/spike/q4_fingerprint.py`), against the other lines of the same file at the pool's commit:
- **The whitelist narrows only to about half the file.** A median **43%** of a file's code lines carry a token the
  whitelist can mutate (a comparison, `not`/`and`/`or`, `True`/`False`, `break`/`continue`, an integer literal), so a
  hunter who knows the generator's rules narrows a file only to about 2.3× fewer lines.
- **Within those lines, a pooled line is unremarkable.** Its length percentile ranges from 0 to 94 (median 32), its
  indentation percentile from 16 to 87 (median 46.5), and its enclosing function's complexity percentile from 14 to 85
  (median 42).
- **None of the signals tested gives the bug away.** No cheap static signal tested here picks pooled lines out. The one
  signal that does narrow the search, coverage by the failing test, costs a coverage run and is exactly the code-reading
  work being measured.
- **This is a fingerprint check, not a guarantee.** Signals not tested, such as blame recency on a repository with a
  long history, could still leak.

## Part C: the session report (`bugpool/report.py`, `python -m bugpool report record.json --out page.html`)

A session record rendered as one self-contained page in the style of the Phase 4 claim evidence report. It reuses
that page's CSS and its traceability check, so every number on the page is a field of the record and the render fails
otherwise.

The sections, in order:
1. **Candidate:** module, operator and family, killing test, candidate id.
2. **Localisation on this candidate:** participant and stranger side by side, each with found or not found (a word
   and a shape) and localisation cost (or "unbounded: the bug was not localised within the budget").
3. **Participant actions:** the action log as a timeline: seconds since start, type, path, pattern or selector.
4. **The planted change:** the diff, marked reviewer-facing, "shown only after the session ended; the participant
   never saw it".
5. **Recorded, not compared:** status, answer, granularity and statement span, exact-line hit, actions used, files
   opened, the participant's elapsed time.

**Hard rules, each enforced by a test:**
- no score, rank, hire, percentile, faster, slower, beat, rating or grade anywhere on the page;
- elapsed time appears only in the recorded section, never in the outcome section beside the stranger's (the
  stranger's elapsed time is not shown at all);
- no aggregate across candidates;
- a refused session renders as the recorded refusal, with nothing planted.

Checked in a browser at phone width in dark mode: no horizontal scroll, no external resources.

**Session record changes:**
- a `headline` block with `localisation_cost` and `found` for both sides, and its definition;
- `candidate.module`, `line` and `statement_span`;
- `planted_diff`, reviewer-facing, written at the end;
- the outcome adds `granularity: "statement"`, `correct_exact_line` and `localisation_cost`;
- `distinct_files_opened` stays as a recorded field;
- the stranger's curve now counts the budget as actions before the answer.

Not built this round, as instructed: the characterization / record-replay oracle.

# Phase 5, round 5: claim-targeted bug selection (2026-10-13)

The two tracks are joined. Phase 4 says what the résumé claims about which code; Phase 5 now plants the bug there.
Module: `bugpool/claims.py`.

## How it works

**Input.** The Phase 4 claim-verdict record: the same `ClaimEvidenceReport` JSON the evidence table renders from. It is
passed to `generate_pool(claims=...)` / `bugpool generate --claims`, and to `Session.start(claims=...)` /
`bugpool session --claims`.

**Marking.** A pool candidate is *claim-cited* when its module (path) is a row's cited path. Each candidate gets a
`claim_target` holding:
- whether its module is cited;
- every citing claim's id, kind, verdict and cited lines;
- whether the bug is inside the cited span.

The candidate carries no claim text.

**Ranking, strongest claim first:**
- rank 0: supported;
- rank 1: inconclusive;
- rank 2: any other verdict that cites the module (contradicted, ...);
- rank 3: uncited.

Within a rank:
- a bug inside the cited lines comes before one elsewhere in the module;
- then claims about the work itself (ownership, metric, other) come before technology_use citations. A "Python" row
  cites one file among many.

The session picks at random (seeded) *within the best tier*.

**Generation.** `generate_pool` validates claimed modules first, which matters under the validation cap. Every gate
is unchanged, including the diversity caps.

**No claimed module.** When no candidate lands in a claimed module, the session plants nothing. It records
`result: "no_claimed_module"` and the message "no validated bug available in a module this résumé claims." This is a
result, not a failure. Planting elsewhere needs an explicit `--allow-uncited`, and the record then says so.

**On the report.** A **Claim targeted** section sits beside the candidate. It says what the résumé said (the claim
text, its kind and id), gives the Phase 4 verdict and the cited module and lines, then where the bug was planted
(inside or outside the cited lines) and what happened (status and localisation cost).

**The Shield rule.** Claim text never reaches the hunt:
- Candidates and the pool carry claim ids only. The text is looked up from the record when the session record is
  written, after the hunt.
- `Session.view()` refuses (`ShieldError`) if any claim text of 20 or more characters appears in what the participant
  is shown. A sentence the repository itself contains is repository content, not a leak.
- The stranger's prompt is built only from `view()`.

**Tests** (`bugpool/tests/test_claims.py`, 6):
- the ranking;
- planting in the strongest tier, with the claim on the record and the report;
- no claimed module, which plants nothing and leaves no worktree; `--allow-uncited` plants only when asked;
- the Shield guard, which catches a planted claim text and spares the repository's own words;
- generation validating the cited span first under three seeds;
- a failed start leaving no worktree.

## Measured on the Crucible pool

The pool is the 7 candidates of the round-4 re-screen: the 9-candidate pool `c6cf5d52`, minus the 2 the plant check
refuses. The claim record is `reports/e2e_crucible_G_run3.json`.

**1 of 7 lands in a claimed module.** It is the same in all three G runs.
- Record commit `2bffef4a` is an ancestor of the pool commit `c6cf5d52`.
- The cited modules are unchanged between the two commits.

| Rank | Candidates |
|---|---|
| supported | 1: `backend/shield/pdf_security.py:123` |
| inconclusive | 0 |
| other cited verdict | 0 |
| uncited | 6: `claims/split.py`, `shield/fonts.py`, `evidence/question_surface.py`, `evidence/target.py`, `shield/analyze.py`, `grounding/html.py` |

**The one hit is weak.**
- **What the claim is.** It is the "pypdf" technology_use row (`s3u0.c7`, supported). That row cites line 25 of
  `pdf_security.py`, an import, while the bug is at line 123, outside the cited span.
- **The strong claims go untargeted.** The résumé's ownership claims cite `evidence/blame.py` (supported) and
  `shield/unicode_scan.py` (contradicted), and no surviving candidate is in either. The pool did hold a `blame.py`
  candidate, but it was one of the 2 the round-4 form check removed, along with `resolve.py` (cited by the Pydantic
  row).
- **The cause.** The pool was generated with no knowledge of the claims and capped at 3 per module, so it spread
  across uncited modules. Generating with `--claims` validates the cited modules first; that run is not done yet.

One live session ran end to end on this pool with the claim record:
- It planted at `pdf_security.py:123`, claim-targeted.
- No claim text reached the view.
- The record's `target` names the claim, the verdict, the cited lines and the outcome.
- The report rendered with its traceability check, at `reports/claim_targeted_session_example.html`.

## Fixed on the way

- **A relative test interpreter.** `Session.start` failed on one (`--python .venv/bin/python`), because tests run
  with the worktree as their working directory. Fix: the path is made absolute at start.
- **A stray worktree.** A failure after the worktree was created left it behind. Fix: any exception during start now
  destroys it.
- **Writing into the target repo.** The stranger's `Session.end()` burned its planted lines into
  `<target repo>/.bugpool/`. That wrote into concurrent_reg; I removed it, and tracked files were never touched. Fix:
  `end(burn=False)` is the stranger's path. A line shown to a model is not a line a participant has seen.
