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
