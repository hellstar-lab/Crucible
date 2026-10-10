# Claim extraction (Phase 4A)

Resume text -> atomic, traceable, typed claims. **Extraction only**: no verdict, no score, no hire signal.
Implements the Claimify method (four stages, two of which may refuse) plus our own checkability routing.

```
python scripts/extract_claims.py resume.pdf --decomposition-tier nano [--json out.json]
python scripts/eval_extraction.py --tiers nano,super,ultra      # labelled rows only
```

## Layout (`backend/claims/`)

| module | stage | model |
|---|---|---|
| `split.py` | 1: split the 2C structure block into units + context; contact lines removed | none |
| `stages.py` | 2 selection, 3 disambiguation, 4 decomposition: rubric prompts, strict schemas | Nemotron (tier per stage) |
| `judgement.py` | the judgement contract: evidence_sufficient, sufficiency_reason, verdict (+unclear), reason; code override | none |
| `support.py` | typed fabrication checks: fabricated_quantity, fabricated_entity (hard fail); ratio as diagnostic | none |
| `routing.py` | 5: kind + checkable_from_code + evidence_kind / why_not_checkable | none |
| `pipeline.py` | Shield gate, per-unit flow, exclusions, cost cap, counters | n/a |
| `config.py` | caps | n/a |

Output: `ClaimExtraction` (`backend/contracts.py`). Every unit and claim carries `source_span` into
`document_text`; every unit ends as claims or as an `ExcludedUnit` with a stage and a reason code.

## Guarantees

- **Shield gate:** extraction runs on the 2C structure block, which holds visible text only. With any finding or
  hidden fragment, units still containing a fragment's text are removed before any call; the Shield facts are
  carried into the output. Contact lines (email, phone, URL) never reach a model.
- **Untrusted text:** every unit and every context string goes through `wrap_untrusted`. The nonce is
  HMAC(key, source, text) when `CRUCIBLE_NONCE_KEY` is set (256-bit hex, environment only), otherwise random.
- **Stage enums and override:** the 4A stage enums are complete partitions with no abstain value. Selection is
  has_verifiable_content | no_verifiable_claims | mixed. Disambiguation is resolved | cannot_be_disambiguated.
  Decomposition is decomposed | no_verifiable_claims (Claimify's own partition). "unclear" exists ONLY in the 4C
  judgement enum, and so does the evidence_sufficient -> unclear override (`judgement.py`).
- **Stage 3 tier: ULTRA, PROVISIONAL (close-out C1).** Stage 3 runs as an n=3 ensemble at temperature 0.7 with
  unanimity. Over w3 and the two units wrongly refused in round 1 (2 repeats each), it was unanimous 1/6 on nano, 5/6
  on super and 6/6 on ultra, and decided correctly 3/6, 5/6 and 6/6. Provisional on near-zero volume: after the
  pre-gate, Stage 3 almost never fires (0 calls on both fixtures), so ultra's ~3.3x price over super barely matters.
  Revisit if Stage 3 starts firing often. Ultra's temperature-0 determinism (FEEDBACK.md #13) is NOT the reason: it
  does not transfer to the temperature-0.7 sampling the ensemble uses.
- **Reason before verdict is the default (C2)** for every 4A stage schema and the 4C judgement schema
  (`--no-reason-first` keeps verdict-first for A/B). Measured on nano, 5 runs each, nothing else changed: w3
  P(cannot_be_disambiguated) went from 0.53-0.62 to 1.0 on every run, and w4 stayed 0/5 but its 4 distinct outcomes
  collapsed to 1. Consistently wrong is diagnosable; randomly wrong is not.
- **no_evidence vs inconclusive (C4).** With the label rubric, all three tiers chose `no_evidence` over
  `inconclusive` on the README-says-fast case. That is CORRECT: "we looked and found nothing" is a different state
  from "we cannot determine it". The earlier three-value enum (supported / unsupported / unclear) merged the two, so
  "unsupported" meant both "contradicted" and "no evidence", and "unclear" meant both "no evidence" and "cannot tell".
- **NLI is not a claim gate** (`docs/nli-gate-rejected.md`). It is used only for Stage 3 resolution equivalence and
  semantic entropy.
- **Stage 3 pre-gate:** a unit with no pronoun or definite reference ("it", "they", "this", "the same", "the
  service", ...) is resolved without a model call.
- **Nullable fields:** fields that do not apply to the chosen label are nullable and the rubrics ask for null, so
  `sentinel_normalized` counts only a REQUIRED field filled with a statement about its own emptiness.
- **Every reply:** `llm.call` runs the refusal detector (G4) and the sentinel normaliser (G3) on it. The pipeline
  then applies the typed support check (rule 3: content not derivable -> `injection_suspected`, unit excluded) and
  an instruction check. Stage 4 claims are checked against the unit and its Stage 3 output only, never the
  surrounding context, so a claim cannot borrow a neighbouring bullet's content.
- **Output guards, after every stage** (`guards.py`; each excludes the unit and increments its counter):
  - `rubric_leak`: a distinctive phrase (4 content words in a row) of a registered rubric example
    (`config.RUBRIC_EXAMPLES`) that the document did not supply.
  - `ungrounded_qualifier`: a closed-lexicon intensifier (proficient, expert, advanced, extensive, senior, deep,
    strong, skilled, seasoned, highly, and their word families) absent from the source unit.
  - `fabricated_quantity` / `fabricated_entity`: the typed support check.
  - `injection_suspected`: instructions instead of content.
- **No prompt contains an eval-set string.** `tests/test_claims_prompt_hygiene.py` cross-checks every registered
  example and every rubric against every eval row, and fails on overlap. It found the S3 premise example
  ("Engineer, Contoso Freight Jul 2018 - Feb 2021") verbatim in eval row w3's context; the example was replaced.
- **Qualifiers:** a claim that keeps a quantity but loses the scope attached to it ("70% *on the read path*") is
  excluded as `qualifier_dropped`.
- **Settings:** temperature 0 and thinking off everywhere; each call logs a prompt hash with nonces masked.
- **Cost cap:** ~40 nano calls per document by default ($0.00504). Over the cap the result is partial,
  `truncated=True`, and the units not reached are listed.
- **Not measured:** unmeasured is `None`: a document with no text layer gives `units = claims = excluded = None`.

## Decomposition rule (Stage 4)

"Negate one part. If the other part's truth is unaffected, SPLIT. If negating it leaves the remainder incoherent,
KEEP TOGETHER."

| | input | output |
|---|---|---|
| SPLIT | "Built a Redis-backed caching layer serving 12k req/s" | "Built a Redis-backed caching layer" and "The Redis-backed caching layer serves 12k req/s" (referent duplicated so each claim stands alone) |
| SPLIT | "Reduced latency 70% by redesigning the PostgreSQL access layer" | the means clause is independently verifiable from code and is the MORE checkable half |
| SPLIT | "Migrated to Kubernetes and Terraform" | "Migrated to Kubernetes" and "Migrated to Terraform", verb carried |
| KEEP | "Reduced p95 latency by 70% on the read path" | "on the read path" is a SCOPE, not an assertion; negating it makes the remainder a different claim |

The Stage 4 PROMPT states the same rule with structurally identical examples using different sentences (Kafka
event bus, Docker layer caching, Loki/Grafana, cache hit rate on the search path). Two of the examples above are eval
rows (w1, w4), and an eval row quoted in the prompt measures memorisation, not the rule.

## Evaluation runs

The platform is not deterministic at temperature 0 (FEEDBACK.md #10). `eval_extraction.py` runs every row
`--runs` times (default 5) and reports "k of n passed" with the distinct outputs of any non-unanimous row.

## Tier comparison protocol (S7)

`scripts/eval_extraction.py --compare-stage4 nano,super,ultra --runs 3`: Stage 2 and Stage 3 run once on nano, are
cached to `tests/eval/stage23_cache.json`, and are replayed byte-identically into every Stage 4 arm, so only
Stage 4's tier varies. It refuses to run until `tests/eval/FROZEN.sha256` matches the eval set.

## Measured

Round 1 (2026-10-11, 84 calls, $0.0079):
- **Key order:** 117/117 replies came back in schema property order (70/70 in Phase 4.0).
- **A nearest-noun guess was caught:** one Stage 3 reply rewrote "on the read path" as "on the Redis-backed caching
  layer"; the qualifier guard excluded the claim.

Round 2 (2026-10-11, after S1-S6):
- **The platform is not deterministic at temperature 0, whatever the nonce.** With the HMAC nonce (byte-identical
  prompts, identical masked hashes), 5 runs of w4 on nano gave 3 distinct selection replies and 5 distinct
  decomposition replies. The random nonce was not the cause of the round-1 variance. Eval numbers need repeated runs.
- **w3 "Improved it by 40%" with "unclear" removed:** Stage 3 on super and ultra chose cannot_be_disambiguated
  3/3 each. On nano it was 2/5; the other 3 runs GUESSED ("the role", "the experience", and "the command-line tool
  for log search", which is a rubric example).
- **w4 on nano, new rubric:** 0/5. Decomposition split the scope "on the read path" into its own claim ("The read path
  experiences the latency reduction"), and the qualifier guard excluded the remaining "Reduced p95 latency by 70%".
- **Rubric examples copied into outputs:** besides the w3 case, a role line produced "Engineered a Kafka-based event
  bus handling 5k events/s", the Stage 4 example. The typed check caught that one (5k, Kafka); the w3 case has no
  quantity or entity, so no check catches it.
- **Stage 3 tier: super, PROVISIONAL.** Measured on n=3, one case (w3 "Improved it by 40%"): nano chose
  cannot_be_disambiguated 2/5 and guessed a referent 3/5; super 3/3 and ultra 3/3 were clean. That round's prompt
  also contained w3's own context line (since removed, see T5), so even this case is not clean evidence. The frozen
  eval confirms or overturns the choice. Super rather than ultra: the same result at about 1/3.3 of the price, and the
  pre-gate made Stage 3 nearly free. Ultra is wired as an escalation (`stage3_escalation="ultra"`): when a guard fires
  on super's rewrite, Stage 3 is asked once more on ultra. It is off by default.
- **Stage 3 pre-gate:** skipped every Stage 3 call on resume_standard.pdf (9) and docx_hidden.docx (1). Calls fell
  25 -> 18 and 13 -> 8.
- **Sentinels:** `sentinel_normalized` went 20 -> 0 (resume_standard.pdf) and 16 -> 0 (docx_hidden.docx). Strict mode
  accepted `"type": ["string", "null"]` on every call.

## Phase 4A-plus P2 (2026-10-11, 71 calls, $0.0173)

- **Enum re-ranking (P2-1, `llm.py`).** Every enum field under a strict schema requests logprobs (top 20). The answer is
  the most probable VALUE, summed over its tokenisations. It replaces the generated value only when its lead exceeds
  the unresolved mass, and the full distribution is recorded (these are shares among grammar-allowed tokens). On the
  real nano reply it turns the generated "unclear" (0.302) into "unsupported" (0.696).
- **Judgement verdict (P2-2, 4C only):** supported | contradicted | no_evidence | inconclusive, with distinct first
  characters. With the label rubric, the T3 neutral case gives no_evidence 0.55 / 0.95 / 0.75 and inconclusive
  0.38 / 0.02 / 0.24 (nano / super / ultra). Without the rubric, ultra put 0.84 on `contradicted`.
- **Stage 3 ensemble (P2-3):** one request, n=3, temperature 0.7, unanimity. Over w3 and the two units wrongly
  refused in round 1 (2 repeats each), it was unanimous 1/6 on nano, 5/6 on super and 6/6 on ultra, and decided
  correctly 3/6, 5/6 and 6/6. Nano's non-unanimous samples included rubric-example referents ("The service is the
  Payments API"); the rule turned them into refusals. Stage 3 stays on super: nano is clearly worse, and 5/6 vs 6/6
  cannot be told apart at n=6.
- **Reason before verdict (P2-4, nano, single Stage 3 call, 5 runs).** w3 went from 4/5 to 5/5:
  P(cannot_be_disambiguated) rose from 0.53-0.62 to 1.0 in every run. w4 stayed 0/5, but the number of distinct
  outcomes fell from 4 to 1. Off by default (`--reason-first`).
- **NLI backstop (P2-5):** over 64 recorded claims from resume_standard.pdf, NLI agreed with the guards on 13 of their
  15 catches. Every claim NLI caught that the guards missed (8) was a false positive: bare skill names ("Go",
  "PostgreSQL") from a skills list, which NLI scores neutral. Not wired as a gate. Note that with "unit + Stage 3
  output" as the premise, NLI cannot catch a guessed Stage 3 referent, because the guess is in the premise.
- **Semantic entropy (P2-6, w3):** nano 0.637 and 0.637; super 0 and 0; ultra 0 and 0.

## Known gaps

- **Bare date ranges (Phase 4C F3, fixed deterministically).** A unit or a Stage 4 claim whose entire content is a date
  range ("2014 - 2018", "Jan 2020 - Present", "03/2020-12/2021") is a field value, not a claim. Units are excluded before
  Stage 2 as `no_verifiable_claims` ("bare date range belongs to the timeline extractor"), with no model call. Claims
  Stage 4 produces from a longer unit are dropped and recorded in `ClaimUnit.dropped_claims`. The live failure came from
  the second path: the unit was "B.Sc. Computer Science, University of Example 2014 - 2018". Like role lines, these
  belong to 2C's timeline extractor (`resume.timeline`).
- **Role lines:** Stage 5 types role lines ("Senior Engineer, Northwind Payments") as `other` and loses their dates.
  This is a routing bug: role lines belong to 2C's timeline extractor (`resume.timeline`), not the claim pipeline.
- **Ungrounded words:** Stage 4 adds words the source does not say ("Proficient in Python" from a skills list). The
  typed check ignores common words by design.
- **Prompt-example leakage:** a resolved referent copied from a rubric example ("the command-line tool for log
  search") is not caught by any check.
