# Negative result: an NLI entailment gate for extracted claims

**Decision (2026-10-11):** a local NLI model is NOT used as a gate on extracted claims. It stays in the project (pinned,
Apache-2.0, optional at runtime) only for Stage 3 resolution equivalence and semantic entropy.

This is the project's second documented negative result. The first was the hidden-text classifier
(`docs/classifier-experiment.md`).

## The hypothesis

Our deterministic guards all approximate one question: does the source unit ENTAIL this claim? They are
`fabricated_quantity`, `fabricated_entity`, `ungrounded_qualifier`, `rubric_leak` and `qualifier_dropped`. A general
NLI check could answer that question directly and catch what the guards miss. The guards would stay as fast
pre-filters, and NLI would be the backstop.

## What we ran

- **Model:** `cross-encoder/nli-deberta-v3-base` (Apache-2.0, revision `6c749ce`), run locally on CPU.
- **Inputs:** premise = the source unit plus its Stage 3 output; hypothesis = the claim. A claim counts as supported
  only if every sentence is classified "entailment".
- **Why this model:** MiniCheck Flan-T5-Large was the alternative. It is ~3 GB; the development machine had ~4 GB free.
  The clustering use also needs a three-way NLI model.
- **Data:** every claim from four recorded extraction runs on `tests/fixtures/docs/resume_standard.pdf`, both claims
  that were kept and claims the guards excluded. 64 distinct claims, checked offline with
  `scripts/compare_guards_nli.py`.

## The four counts

| column | count | contents |
|---|---|---|
| guards catch | **15** | 3 claims a role line borrowed from the next bullet ("Migrated a card-settlement service to Kubernetes"); 9 "Proficient in X" from a plain skills list; 2 scope losses; 1 rubric-example leak ("Engineered a Kafka-based event bus handling 5k events/s") |
| NLI catches | **21** | 13 of the guards' 15, plus the 8 below |
| **NLI catches, guards miss** | **8** | **all false positives**: the bare skill names "Go", "PostgreSQL", "Redis", "Docker", "Kubernetes", "Terraform", "AWS" and "Python is listed as a skill", from the unit "Python, Go, PostgreSQL, Redis, Docker, Kubernetes, Terraform". Each is true to the source; NLI scores them "neutral" because a bare noun is not a proposition the premise can entail. |
| guards catch, NLI misses | **2** | real catches: "Backend engineer with 6 years of experience" and "I have 6 years of experience building payment services." drop the scope "in Python and Go". NLI rates both entailed (0.99), because dropping a qualifier yields a WEAKER statement, which a premise still entails. |

The deciding column, what NLI catches that the guards miss, contained no true catch. A gate built on it would have
excluded 8 correct claims and added nothing.

## Why it cannot catch a guessed referent

The failure Claimify exists to prevent, and the one no deterministic guard catches, is a guessed referent:
"Improved it by 40%" rewritten as "Improved the project by 40%". By design the premise contains the Stage 3 output, so
it contains the guess. The claim is then entailed by its own premise, and the check passes. Dropping the Stage 3
output from the premise does not help either: the original unit ("Improved it by 40%") does not entail any resolved
version of itself, so every correctly resolved claim would fail too. NLI is structurally blind to the one error we
most needed a backstop for. That error is handled upstream instead: the Stage 3 n=3 ensemble with unanimity turns
disagreeing resolutions into refusals.

## Limits of this result

- One document, synthetic, 64 claims. It shows that the backstop added nothing here and fails in specific ways, not
  that NLI is useless for claim checking in general.
- The two misses on scope loss are a known property of entailment (a weaker claim is entailed), not a model defect;
  the `qualifier_dropped` guard exists for exactly that.
