# Evidence retrieval (Phase 4B)

Each 4A claim becomes an `EvidenceSet` (`backend/contracts.py`): ranked repository evidence, capped at 10 items, ready
for 4C to judge. Deterministic: no model calls, no network, no embeddings.

```
python scripts/retrieve_evidence.py --claims claims.json --evidence repos.json --json out.json \
    --user LOGIN --email ADDR --checkout owner/repo=PATH
python scripts/retrieval_lexicon_coverage.py
```

## Invariants

- **Empty results.** `items == []` with `searched` populated means **we looked and found nothing**, which is evidence
  for no_evidence. `searched` = None means **we did not look**, which is no evidence at all.
- **Authorship.** `authored_by_candidate` is decided here, from the data, and never inferred later. A technology that
  exists in a repository is not evidence that the candidate used it. On a single-commit import
  (`authorship_confidence` "none") the per-file ratio is reported, but `authored_by_candidate` is None.
- **Exclusions.** Excluded code is never evidence. Every item is checked against the 3.5 exclusion list and the scope
  (`sources.assert_eligible`, which raises `ExclusionViolation`). That covers vendored, generated, minified,
  documentation, binary and duplicate files.

## Inputs

The 3.6 `TargetReport` alone cannot feed the rules: it carries no file list, imports, contents or per-file authorship.
Retrieval therefore also reads:
- **The identity-free snapshot** that 3.6 caches in `.cache/evidence`, keyed by `head_sha`.
- **The candidate identity, passed again.** The report stores emails redacted, so it cannot be rebuilt from the
  report. Per-file authorship is recomputed and checked against the report's `authored_source_lines`. If the totals
  differ, every item has `authored_by_candidate` = None and the set says why.
- **An optional checkout**, used only for verbatim excerpts (`git show <head_sha>:<path>`). Without one, items carry
  no excerpt or line numbers, and the rules that need file content say so in `rules_fired`.

## Rules (`backend/retrieval/rules.py`)

| kind | rules |
|---|---|
| technology_use | `r_dependency` (exact, then rapidfuzz ratio >= 90, recorded), `r_import` (tree-sitter, line-regex fallback, recorded), `r_config` (file patterns and config keys from the lexicon) |
| ownership | `r_ownership` (functions and countable files whose name matches the claim's artifact words) |
| artifact_exists | `r_structure` (repo_facts.structure, minus excluded paths), `r_config` |
| scale | `r_scale` (the repository's numbers next to the claimed ones; no comparison) |
| timeline | `r_history` (overlap of the claim's dates with first and last commit) |
| metric | `r_benchmark` (benchmark, perf and load-test files; none -> `no_benchmark_present`) |
| role_or_team, outcome, other | not searched: `retrieval_performed` False with a `skip_reason` |

Ranking: exact before fuzzy, then authored by the candidate (True > None > False), then BM25 (bm25s) against the
claim's words.

## Measured

- **Lexicon (`backend/retrieval/data/tech_artifacts.json`).** 59 technologies: 55 with a code artifact, 4 known to leave
  none (Linux, Unix, REST, Metasploit). The fixtures name 58 distinct technologies; 56 are covered. Unknown: "Beam" and
  "R", single mentions from the arXiv paper ("beam search", the letter R), likely detection noise.
- **Detection gap upstream of 4B.** Terms in the external CVs' skills sections that neither lexicon recognises:
  Rancher, Packer, Hugo, LESS, OCaml, LaTeX.
- **Exclusion stress.** Redis appears only under `vendor/`, `third_party/`, `node_modules/`, a generated `*_pb2.py`, a
  minified bundle and `docs/`. Result: zero items, with the search recorded (7 excluded files skipped).

## Open question

GitHub Linguist's `vendor.yml` treats `.github/` as vendored, so 3.5 excludes `.github/workflows/*.yml`. Under the
strict rule (evidence only from files that survived 3.5), a CI workflow is never evidence, and a claim like "set up
CI with GitHub Actions" comes back as "looked and found nothing". `r_structure` records the drop
(`r_structure:excluded_by_3.5=N`) so it is visible, but the item is not returned.
