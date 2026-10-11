# Interview questions (Phase 4G)

`backend/questions/` turns the evidence table into interview questions. In `scripts/crucible.py` it runs by default:

- `--max-questions N` (default 8);
- `--include-unverifiable`;
- `--per-student EMAIL`;
- `--no-questions`.

The questions are stored in the report JSON (`ClaimEvidenceReport.questions`) and shown in the page's
"Interview questions" section.

This phase carries load for two things:
- The two-tier contradiction rule depends on it: a contradiction that was not confirmed becomes an inconclusive row,
  and an inconclusive row must become a QUESTION, not an accusation.
- The PBL pilot needs per-student question sheets.

## G1: code selects, the model only phrases

`questions.select` decides, deterministically and with no model, four things:
- which rows are asked;
- the shape of each question;
- the anchor each may talk about;
- the order.

One request (`question_generation`, Super, n=1, temperature 0) writes the sentences. **The model is never told the
verdict**, so it cannot repeat it or lean on it. A test asserts that no verdict word reaches the prompt, and that
unauthored code never does either.

A bad question is simply unused. It is not a wrong verdict, so there is no ensemble and no retry.

## G2: shape by verdict

| Verdict | Shape | Anchor |
|---|---|---|
| supported | `code_decision`: a technical question about a decision visible in the code | a function the person wrote, with its line range and body |
| contradicted | `role`: "Tell me about your role on <artifact>." | the claim's object phrase (the claim minus its leading verb) only: **no code**, because the cited code is someone else's (R1) |
| no_evidence | `where`: where did that work happen | the claim only |
| inconclusive | `walkthrough` | an authored function if any evidence item is in the person's code, else the claim only |
| unverifiable | `conversation` | the claim only; asked only with `--include-unverifiable` |

How a supported row finds its anchor:
- The candidate files are the cited file, or, for technology_use, every file holding an evidence item in the
  person's code (the same reasoning as L1).
- An eligible function is one the person wrote at least 90% of (`ANCHOR_RATIO`, the bar Phase 3.6's anchors use).
  For ownership it must also overlap the cited span.
- The best is picked by lizard CCN x authored ratio, and no function anchors two questions.
- For a library claim (import evidence), the function must also USE the technology: its body has to reference a
  name the import binds (`from rapidfuzz import fuzz` -> `fuzz`). Measured: before this, "RapidFuzz" was anchored to
  the most complex function in a file that merely imports it. A language claim ("Python") takes any authored function.
- If nothing qualifies, the row is "not asked", with the reason. It is never anchored on someone else's code.

**Near misses (I1).** A contradiction one tier did not confirm (`contradiction:not_confirmed`) is inconclusive for the
reviewer, but it is asked exactly like a contradiction: group 1, the neutral role question, never cut by the cap.
Measured in a live run: it ranked with ordinary inconclusive rows and the cap removed it, so the near miss was neither
alleged nor asked. That breaks the stance the two-tier rule depends on ("or the claim becomes a question for the
interview").

## G3: four hard rules, on every phrased question

A failing question is dropped and listed under "rejected", with its rule.

- **R1, only authored code.**
  - The question may name no repository path or function outside its anchor. A helper visible in the anchor's own
    body counts as part of the anchor.
  - The anchor is re-checked from blame as at least 90% the person's.
- **R2, no accusation.** A lexicon: "you claim", "falsely", "actually", "really wrote", "why did you say",
  "allegedly", and close variants. This is what makes the two-tier contradiction rule safe.
- **R3, no verdict.**
  - It rejects verdict words, plus phrases about the checking itself: "we found", "the evidence shows",
    "blame shows", "could not verify".
  - It uses phrases rather than bare words, because the code being asked about may itself be about blame or evidence
    (Crucible's own `blame.py`).
- **R4, no fact outside the anchor.**
  - These are the EXISTING typed fabrication guards (`backend.claims.support.check`: fabricated_entity,
    fabricated_quantity), run on the question against the anchor text (claim + location + function + body).
  - They run one sentence at a time, because the guard treats only a text's first word as sentence-initial, and a
    question's second sentence ("What made ...") would otherwise read as a proper noun.

## G5: order and cap

1. contradicted and I1 near misses: always included (never cut by the cap), always neutral;
2. supported, by CCN x authored_line_ratio, highest first;
3. no_evidence, technology claims first, then other kinds (for example a metric with no benchmark: "where did that
   latency work happen?" is a useful question);
4. inconclusive;
5. unverifiable, only with the flag.

Ties keep the report's row order. The cap (default 8) applies after group 1, and records `dropped_by_cap` and
`dropped_claim_ids`. The engine also records `rejected_count` and `skipped_count`, so the page formats them and never
counts.

## G6: per student

`--per-student EMAIL` recomputes function authorship from the snapshot's blame for that address alone, then runs the
same engine. R1 then applies per student: a student's sheet holds ONLY questions anchored in code they wrote.
Claim-only questions (role, where, conversation) are left off a student sheet.

## Measured R4 rejections (live)

R4 is strict, by design: a rejected question is unused, not wrong. These came from the Crucible resume runs:
- "O(n²)": the "O" read as a name;
- "XML": absent from the function body;
- "Exception": absent from the function body;
- "50%": a threshold the model paraphrased from code that writes it as `0.5`.

A sharper question would sometimes have passed, but the guard does not try to tell paraphrase from invention here.
