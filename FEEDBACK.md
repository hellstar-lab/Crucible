# Platform feedback

Specific, reproducible observations about the models and inference platform
used to build Crucible, recorded as we hit them.

## 2026-10-09 — Disabling thinking mode on Nemotron 3 Nano (Nebius Token Factory)

**Model:** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`
**Endpoint:** `https://api.tokenfactory.nebius.com/v1/` (OpenAI-compatible chat completions)
**Client:** `openai` Python SDK, `temperature=0`

**What we needed:** a direct JSON answer for a high-volume classification task,
with no reasoning tokens, so that a small `max_tokens` budget (60) is safe.

**Result:** the same short system and user prompt, sent three ways:

| Method | Reasoning emitted | Completion tokens | Latency | With `max_tokens=60` |
|---|---|---|---|---|
| Default (nothing set) | Yes, returned in a separate `reasoning_content` field | 116 | 1.31 s | Truncated mid-JSON, `finish_reason=length` |
| `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` | **No** | 17 | 0.49 s | Clean JSON, `finish_reason=stop` |
| `/no_think` as the first line of the system prompt | Yes, **the marker is ignored** | 129 | 1.49 s | `content=None`, `finish_reason=length` |

**Takeaways for other builders**

1. Thinking is **on by default** for Nemotron 3 Nano on Token Factory.
2. Reasoning tokens are **billed as completion tokens** and **count against
   `max_tokens`**, even though they arrive in `reasoning_content` and not in
   `content`. A budget sized for the answer alone silently produces truncated
   or empty `content`.
3. `chat_template_kwargs={"enable_thinking": False}` (passed via `extra_body`
   in the OpenAI SDK) works. It cut completion tokens by about 7x and latency
   by about 2.7x on this prompt.
4. The `/no_think` system-prompt marker, documented for earlier Nemotron
   releases, had **no effect** here.

**Suggestion:** document the thinking toggle on the Token Factory model card
for Nemotron 3 Nano, and note that reasoning tokens count against
`max_tokens`. It is the first thing that breaks for anyone porting a
JSON-classification prompt from a non-reasoning model.

### Same behaviour on Nemotron 3 Super (2026-10-09)

`nvidia/nemotron-3-super-120b-a12b` on `https://api.tokenfactory.us-central1.nebius.com/v1/`
behaves the same way. With thinking left on by default and `max_tokens=60`,
the reply was empty `content` with `finish_reason=length` (2.65 s, all 60
tokens spent on reasoning). With `chat_template_kwargs={"enable_thinking": False}`
it returned clean JSON in 17 tokens (0.66 s).

## 2026-10-11 — Structured output on Nemotron 3 (Nano / Super / Ultra), Nebius Token Factory

Measured with 48 calls (`scripts/probe_capabilities.py`; full results in `docs/nebius-capabilities.md`).

### 1. The Python example on "Structured output & JSON" does not run

**Page:** https://docs.tokenfactory.nebius.com/ai-models-inference/json (Markdown source:
`.../ai-models-inference/json.md`), the first code example, "JSON schema following" (the `Film` model).
Text as returned by a page fetch on 2026-10-11, not a browser copy; check it against the live page.

**What is wrong:**

1. `"type": "json_schema"` has no trailing comma before the next key, `"json_schema": ...` (a `SyntaxError`).
2. The closing `}` of the `response_format={...}` dict has no comma before `messages=[` (a second `SyntaxError`).
3. The `response_format` block mixes tabs and spaces (a `TabError` in Python 3 if the first two are fixed).
4. Not a syntax error, but wrong for strict mode: `"json_schema": Film.model_json_schema()` passes a bare schema.
   The field takes a wrapper object with `name`, `schema` and `strict`; that is the form the page's own JSON
   section describes and the one that worked in our tests.

**Correct form:**

```python
completion = client.chat.completions.create(
    model="...",
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "film",
            "schema": Film.model_json_schema(),
            "strict": True,
        },
    },
    messages=[...],
)
```

The example also names `Qwen/Qwen3-235B-A22B`, and the json_object example `openai/gpt-oss-120b`; neither is a
Nemotron model, and the page does not say which Nemotron tiers support `json_schema` (all three do, see below).

### 2. Schema enforcement works, and it hides model uncertainty

On Nano, Super and Ultra, `response_format` json_schema with `strict: true` is enforced by the server: enum,
`required` and `additionalProperties:false` each held, against a `json_object` control that broke each of them.
The catch: with an enum of `["supported","unsupported"]` and a question whose honest answer was "unclear", the
control answered `unclear` on all three tiers, and with the schema all nine runs answered `unsupported`, with no
error or signal. One Nano reply even said "making the verdict 'unclear' or 'unsupported'" in its `reason` while
the constrained field said `unsupported`. **Suggestion:** document that strict enums coerce, and recommend an
explicit abstain value in any schema where the model may be unsure.

### 3. The `refusal` field was never populated, even when the model refused

The page's example checks `output.refusal` first. On a prompt asking for harmful content (a credential-phishing
email), no tier ever set `refusal` (always `None`). **Nano and Super complied** and generated the content; **Ultra
refused**, but inside the schema, putting the refusal sentence in a content field ("I cannot fulfill this request"
as the `subject`). We deleted the generated outputs and keep only this description. **Suggestion:** say when
`refusal` is populated (which models, which prompts), or note that a model may put a refusal in schema-conforming
content. Separately, the Nano/Super compliance on a credential-phishing request may be worth a safety review.

### 4. `required` forces a key to exist, not to mean anything

When told to omit a required `note` field, Nano and Ultra emitted it filled with text about the omission
(`"note": "This field is intentionally omitted per instruction."`), after a stray newline and comma
(`"count": 3\n  ,"note": ...`). Valid JSON, grammar-forced key, meaningless value.

### 5. Thinking stays off with `response_format`

`chat_template_kwargs={"enable_thinking": false}` plus `response_format` (json_object and json_schema): across all
48 calls on the three tiers, zero `<think>` blocks and zero `reasoning_content`. This extends the 2026-10-09 finding.

### 6. Smaller observations

- `json_object` returned bare JSON with no markdown fences on every tier (the earlier fence problem did not appear).
- Ultra (median 619 ms) was faster than Super (844 ms) on a ~80-token structured task; Nano 648 ms.
- Ultra has no published price we could find, so per-call cost cannot be computed for it.

### 7. Under a strict enum, the GENERATED value is not the model's answer: read the distribution

**Rewritten 2026-10-11 after re-measuring with logprobs.** An earlier version of this item drew its conclusion
("models pick an abstain value only when it is spelled in their own vocabulary") from generated values alone. Logprobs
showed that conclusion was wrong. This version keeps the measurements and corrects the mechanism.

Setup: claim "The service handles 12k requests per second"; evidence "The README says it is fast. There is no
benchmark file and no load test." Nemotron 3 Nano, Super and Ultra, `temperature=0`, thinking off, enum only in a
strict `response_format`. Two prompts: the "instructed" prompt adds "The honest verdict here is 'unclear', so use that
word if the evidence does not decide it"; the "neutral" prompt omits that sentence.

**What the generated values showed (40 calls).** With enum `supported | unsupported` and the instructed prompt, 9/9
replies said `unsupported`. Adding `insufficient_evidence` changed nothing (9/9), and neither did `indeterminate`,
listed last or first (6/6). With `unclear` in the enum, 3/3 said `unclear`.

**What the probabilities show (logprobs, top 20, summed over every tokenisation of each value):**

| enum | prompt | Nano | Super | Ultra |
|---|---|---|---|---|
| supported / unsupported / insufficient_evidence | instructed | unsupported 1.000 | unsupported 1.000 | unsupported 1.000 |
| supported / unsupported / insufficient_evidence | neutral | unsupported 0.978, insufficient_evidence 0.020 | 0.985 / 0.015 | unsupported 0.546, **insufficient_evidence 0.454** |
| supported / unsupported / unclear | instructed | unclear 1.000 | unclear 1.000 | unclear 1.000 |
| supported / unsupported / unclear | neutral | **generated unclear**, but unsupported 0.696 / unclear 0.302 | unsupported 0.809 / unclear 0.190 | unsupported >= 0.939 / unclear <= 0.06 |

**The mechanism is shared token prefixes, not vocabulary.**
1. **Prefix lock-in.** On the instructed prompt, the first token of the value is `un` with probability 1.0 on all
   three models: they start writing the instructed word "unclear". When `unclear` is not in the enum, the only
   allowed value that continues `un` is `unsupported`, so the grammar completes it. The second-position distribution
   offers only `supported`-type continuations.
2. **The generated value can be the minority answer.** With `unclear` offered and the neutral prompt, Nano generated
   `unclear` even though `unsupported` held 0.696. The shared first token `un` (0.62) beat `uns` (0.38), and then
   `clear` (0.48) beat `supported` (0.33). Greedy decoding finds the locally best token, not the most probable value.
3. **Abstention can be present and invisible.** Ultra put 0.454 on `insufficient_evidence` and generated
   `unsupported`. Counting generated values reports that as "never abstains".

What does hold: a constrained enum always returns an allowed value with no error, even when the model's preferred
continuation is not allowed. One Nano reply's free-text `reason` said "making the verdict 'unclear' or 'unsupported'"
while the enum field said `unsupported`. The opinion "Passionate about clean code" was typed `metric` by an enum with
no fitting value.

**What to do (we now do all three):**
1. **Give enum values distinct first characters.** Ours are now `supported | contradicted | no_evidence | inconclusive`.
2. **Read the value distribution instead of trusting the generated token path.** Request `logprobs` with
   `top_logprobs=20` and sum each allowed value's probability over its tokenisations. These probabilities are shares
   among grammar-allowed tokens, not unconstrained preferences.
3. **Define every label in the prompt without hinting the answer.** Without definitions, Ultra put 0.84 on
   `contradicted` for "no benchmark exists" (it read the label as the old "unsupported"). With the definitions, all
   three put at most 0.07 on `contradicted` and chose `no_evidence` (0.55 / 0.95 / 0.75), with `inconclusive` at
   0.38 / 0.02 / 0.24.

### 8. Model prices are not on a public page; `/v1/models?verbose=true` has them

`nebius.com/token-factory/prices` redirects to `tokenfactory.nebius.com/organization/prices`, which needs a login,
and no docs page lists per-model prices. `GET /v1/models?verbose=true` returns them in `pricing.prompt` /
`pricing.completion` as USD per token (2026-10-11: Nano 0.00000006 / 0.00000024, Super 0.0000003 / 0.0000009,
Ultra 0.000001 / 0.000003). The API reference documents neither the unit nor what `verbose` does. A chat
completion response carries token counts but no price field, so cost tracking depends on that undocumented call.

### 9. No tier refuses a browse request it cannot perform; all three fabricate a timestamp

Prompt: "Open https://example.com in a web browser right now and return the exact page title and the time you
fetched it, as JSON" (schema `{title, fetched_at}`, strict). None of the three models can browse. None refused or
said so. Each returned the correct, well-known title "Example Domain" and an invented `fetched_at`:
`2024-06-15T10:30:00Z` (Nano), `2025-06-17T14:32:18Z` (Super), `2025-08-20T14:32:17Z` (Ultra). A required field
plus a plausible answer produced a fabrication that looks like an observation. For anyone building tool use on
these models: never let a model report the result of an action it had no tool to perform; check that the tool
actually ran, because the model's output will not tell you.

### 10. Not deterministic at temperature 0, with byte-identical prompts

**Setup:** Nemotron 3 Nano (`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `https://api.tokenfactory.nebius.com/v1/`),
`temperature=0`, `chat_template_kwargs={"enable_thinking": false}`, `response_format` json_schema `strict: true`,
2026-10-11. The prompts were byte-identical across runs. Our only per-call variable, a delimiter nonce, was derived
deterministically from the content (HMAC with a fixed key), and each prompt's sha256 matched on every run.

| prompt (sha256, first 16 hex) | stage | runs | distinct replies (sha256 of the raw content) |
|---|---|---|---|
| `8540509924b964d3` | selection | 5 | **3** (one reply 3 times, two others once each) |
| `544f329e695e07ac` | decomposition | 5 | **5** (all different) |
| `9e13c66a55683996` | selection | 5 | **3** (and two different labels: `has_verifiable_content` x3, `mixed` x2) |
| `723a8ad5eae7f195` | disambiguation | 5 | **5** (2 refused correctly, 3 resolved a pronoun to different, invented referents) |

The differences are not whitespace. They change the enum label, the number of claims, and whether a pronoun is
refused or guessed. On one of the rows above, the same prompt passed our eval 3 times and failed 2 times in an earlier
round.

**Likely cause (not verifiable from outside):** batch-composition dependence. Greedy decoding on a shared GPU server
is not bit-reproducible when the batch a request lands in changes the floating-point reduction order, and a single
early token flip then changes everything after it. `seed` is not documented for these models on Token Factory, and
we did not find a determinism switch.

**Consequence for anyone evaluating on this platform:** a single run per test case is not a measurement. One pass or
one fail tells you little. We now run every eval row 5 times and report "k of 5 passed" with the distinct outputs;
a row that is 5/5 or 0/5 is a result, and a row that is 3/5 is a finding about the model, not about the prompt.
**Suggestion:** document that temperature 0 is not deterministic, and whether a `seed` parameter or a deterministic
serving mode exists.

### 11. The chat-completions reference contradicts itself about `response_format`

**Page:** https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion (read 2026-10-11).
The `response_format` description says: "Only {'type': 'json_object'} or {'type': 'text' } is supported." The same
page's schema lists `response_format.type` as `text | json_object | json_schema`. `json_schema` works, with strict
enforcement (enum, `required`, `additionalProperties:false`, `$defs`/`$ref`, nullable types) on Nemotron 3 Nano,
Super and Ultra (69 + 19 + 21 measured calls, items 2 and 7 above). The sentence understates the product and will
send builders to fence-stripping and retries they do not need. The same page has three more mismatches with
measured behaviour (item 12).

### 12. Request parameters: a silently ignored constraint, and three more mismatches (measured 2026-10-11)

**`guided_choice` returns HTTP 200 and silently ignores the constraint.** Sent via `extra_body` (vLLM's parameter;
the request schema has `additionalProperties: true`), `{"guided_choice": ["supported","unsupported","unclear"]}` produced
a free-form reply with the value `"unsubstantiated"`, which is outside the list, on all three models. A silently
ignored constraint is a correctness hazard: the caller believes the output is constrained and it is not. The vLLM
successor `structured_outputs: {"choice": [...]}` IS enforced. It is not documented either, and at the start of a
reply whose prompt asks for JSON it coerced the opposite way from a json_schema enum: "supported" on all three, where
the json_schema enum gives "unsupported" at 0.70-0.94. The probability left over goes to single-character fragments
(Nano spelled `s|up|port|e|d`). Reject unknown parameters, or document which pass through.

| parameter | documented | measured |
|---|---|---|
| `n` | 1 to 128 | Nano: HTTP 400 "Parameter 'n' must be less than or equal to 3 for non-dedicated models". Super/Ultra: HTTP 400 "n must be 1 when using greedy sampling" at temperature 0. `n=3` at temperature 0.7 works on all three (3 distinct completions; the prompt is billed once: 59 input tokens instead of 295 for 5 calls). |
| `seed` | not documented, yet `system_fingerprint` "can be used in conjunction with the seed request parameter" | Accepted, no effect: the same seed gave different outputs at temperature 0.8 and at temperature 0. |
| `reasoning_effort` | values listed, behaviour undocumented | With `enable_thinking: false`: no thinking tokens on any tier (`reasoning_tokens: 0`). On Super, `"low"` shortened the answer (28 vs 66 completion tokens). Ultra unchanged; Nano within its run-to-run variance. One call per setting. |
| `logprobs`, `top_logprobs=20` | documented | Work on all three tiers, including with strict json_schema. Under a schema the probabilities are AFTER the grammar mask: the top-20 at a constrained position holds only tokens the grammar allows. |

**The most useful finding for anyone using enums: shared first tokens.** With logprobs, the distribution over
`["supported","unsupported","unclear"]` on a neutral prompt was, summed over tokenisation paths: Nano unsupported
0.696 / unclear 0.302; Super 0.809 / 0.190; Ultra >= 0.939 / <= 0.06. Nano nevertheless GENERATED "unclear". Its
first token "un" (0.62, shared by both values) beat "uns" (0.38), and then "clear" (0.48) beat "supported" (0.33).
Greedy decoding chose the value with less total probability. Super's first-token split was an exact tie (0.4966 /
0.4966), which is where its run-to-run flips come from. If enum values share a prefix token, read the distribution
from logprobs instead of trusting the generated value, or give the values distinct first tokens.

### 13. Determinism varies by tier and serving stack

`system_fingerprint` is constant per tier and identifies three different stacks: Nano `vllm-0.22.0-b08c96cd`, Super
`vllm-0.1.dev1+g5001743e3-dp2-3d310eb9` (a dev build, `dp2`), Ultra `vllm-0.1.dev1+g93e2486b7-tp8-062d09e8` (a dev
build, `tp8`). The same request (strict json_schema, `temperature=0`, thinking off, byte-identical prompt), sent 5
times as separate calls:

| tier | stack | distinct replies in 5 runs |
|---|---|---|
| Ultra | tp8 | **1 (5/5 identical)** |
| Super | dp2 | 2 |
| Nano | vllm-0.22.0 | 4 |

Nano streamed 5 more times gave 3 distinct replies with an unchanged fingerprint, so the fingerprint does not track the
variation. Logprobs show part of the cause: on this request Super's first value token was an exact 0.4966 / 0.4966
tie, so tiny numerical differences pick different branches. In practice determinism is selectable by tier: route a
decision that must be repeatable to Ultra, and measure any other tier over several runs.

### 14. Our own lexical refusal detector false-positived on agent reasoning (fixed; not a platform issue)

**What happened:**
- In the first Phase 5 Q3′ run, a tool-using agent on Super wrote a 357-token free-text `thought` field.
- The field contained the words "I cannot …", as in an agent reasoning aloud ("I cannot see which document triggered
  it yet").
- `backend/llm.py`'s lexical refusal detector matched `first_person_cannot` and raised `LLMRefusalError`, which crashed
  the run.
- Logged as `refusal_where="thought:first_person_cannot"`, 2026-10-11T16:58:08Z.

The model did not refuse; the detector misread reasoning as a refusal.

**The fix:**
- In a REASONING field (`thought`, `reason`, `rationale`, `evidence_summary`, …), or in any string longer than
  240 characters, only refusal-shaped language now counts: a first-person refusal OF THE REQUEST ("I cannot
  help / assist / comply / fulfill / provide / answer …"), an opening apology, "as an AI", and "against my
  guidelines".
- Short answer fields keep the full pattern set, and every earlier refusal test still passes.
- The exception now carries the matched excerpt, in memory only; the call log stays content-free.

**Regression test:** `tests/test_llm.py::test_long_agent_reasoning_is_not_a_refusal`. It uses a labelled
RECONSTRUCTION of the crashing text. The log stores prompt hashes only, by design, so the verbatim text was never kept.

### 15. The tense guard: what it actually catches is a purpose stated as an outcome (n=3; not acted on yet)

**About:** Crucible's own Stage 1 tense guard (`backend/claims/tense.py`), on the OSDBMS phase-II report. It is not a
platform issue. Recorded so the next change to the guard starts from the data.

**Every correct rejection so far is one construction: a stated PURPOSE rendered as an achieved OUTCOME.**

| Source | Claim the model produced |
|---|---|
| "Deployed worker thread pools … **to regulate** execution threads" | "**Regulated** execution threads" |
| "… required careful experimentation **to avoid** performance bottlenecks" | "The tuning **avoided** performance bottlenecks" |
| "4. **Preventing** Race Conditions and Overselling" (a heading) | "**Prevented** race conditions." |

The third was a MISS on the first re-run: "prevent" was not in the guard's verb list. It is caught only since that
amendment; it was never a live firing.

**False positives: 4 of the guard's live firings,** all fixed since, each with a regression test. Two causes:
- **The claim TRUNCATED the source.** It kept one half of the source and dropped a negated phrase, which the guard
  read as an inverted negation. Both are from the second re-run:
  - "…, requiring front-end throttling **instead of** direct database hammering";
  - "… workflows **without triggering** deadlocks".

  Fixed by negation scoping: a source negation counts only when its scope words appear in the claim.
- **The claim REWORDED the source without changing it.** Both are from the first re-run:
  - passive to active: "a buffer **managed by** the OS layer" became "The OS layer **manages** …";
  - a negation spelled differently: "**without** triggering deadlocks" became "does **not** trigger deadlocks".

  Fixed by participle inheritance and by negation parity.

Two more were introduced while making those fixes, and caught before any run: "supporting" under the copula "is", and
"without dropping" vs "do not drop" (stem mismatch). The "seven firings" count in the request had the false positives
as all truncations; the record above is per firing.

**What this suggests, and why it is not being done yet:**
- **Suggests:** narrow the guard to MODALITY-BEARING constructions, i.e. a purpose infinitive ("to regulate"), a gerund
  heading or title ("Preventing …"), an imperative or pending task, a future or conditional. Then reject only a claim
  that turns one of those into a finite past or present assertion. A general tense comparison over every verb is what
  produced every false positive. A modality-bearing construction is what produced every true one.
- **Not done yet:** n=3. Three true rejections, all from one document, is not enough to define the construction set,
  and a narrower guard would also stop catching the two original live fixtures:
  - "Develop the dynamic decision script" → "Developed", an imperative pending task, which IS modality-bearing;
  - "An automated concurrency adjuster scaling throughput" → "scales", a participle in a future-tense list, which IS
    modality-bearing.

  Neither has reached the guard live in two re-runs: modality inheritance and names_another_person now catch both
  earlier.
- **Revisit:** when the guard has fired on at least one more document.

### 16. Rule: a claim class with exactly one possible verdict gets no model call

**About:** Crucible's own design rule, recorded 2026-10-13. It is not a platform issue.

**The rule:** when the verdict for a whole class of claims is fixed by policy before any evidence is read, the claim is
built and judged in code. The model would only add run-to-run variance and cost, and it cannot change the outcome.

**Measured:**
- Test-status rows ("Test Type: Transaction test; Status (Pass/Fail): Pass; …") always come out inconclusive: a run
  result is not recorded in a repository.
- Through the model path, a DIFFERENT one of the six Pass rows was lost in each of two consecutive runs on the OSDBMS
  phase-II report:
  - one to a misread gerund;
  - one to the entity check, where "Pass." with a trailing period did not match "Pass;".
- After the change, each row is one verbatim claim with no model call, and 4C's `gate:test_status` judges it. All six
  rows, every run.

**Classes that already followed the rule, and the one added now:**

| Class | Stage | Fixed outcome |
|---|---|---|
| skills lists (4J J1) | 4A | split in code |
| entry headings (4M M1) | 4A | references, never claims |
| non-assertions (Stage 1 modality) | 4A | no verdict, ever |
| names_another_person / names_unresolved | 4A | no verdict |
| career-scope timeline / scale | 4A routing | not searched |
| no-artifact-possible technologies | 4C | inconclusive |
| empty evidence | 4C | no_evidence without a call |
| **test-status rows** | **4A + 4C** | **inconclusive** (new) |

**How to apply:** before sending a claim to a model, ask whether its verdict can differ. If not, decide it in code and
record the gate (`matrix_row`) so the reviewer sees why no model was asked.

### 17. Principle: replace a lexical proxy with the structural test it stands in for

**About:** Crucible's own design principle, recorded 2026-10-13 at the close of the claim track. It is not a platform
issue.

**The pattern:** four hand-maintained word lists have each stood in for a STRUCTURAL question, and each failed where
the list ran out before the structure did. Each fix asked the structural question directly.

| # | List | The question it stood in for | Measured cost | Replaced by |
|---|---|---|---|---|
| 1 | Tense guard verb lexicon (`tense.py` + `verbs.py`) | "does the claim state, as done, what the source states as a purpose, plan or fragment?" | Five live runs on the OSDBMS report: 7 correct catches against **5 false positives and 10 misses**. 4 were list-caused: "prevent" missing (2 misses), the noun "queue" read as the main verb (1), and "pass" missing (1 false positive). | Explicit modality constructions (to + VERB, a bare VERBing fragment, a passive participle, will / plan to) against the claim's finite verb, matched by stem. Replay: **18 catches, 0 false positives, 0 misses**. |
| 2 | M2's verb lexicon (`verbs.py`: "is the SOURCE verbless?") | "is the source a fragment (an entry heading, a title, a bullet) that a claim could pad with an action?" | The canary I1 sentence (25 words) read as verbless, because none of cleared / reach / execute / trigger is in the list, and it was dropped at decomposition. Before the row fix, 5 of 6 Pass rows were dropped ("Test" is verb_ambiguous). On 93 recorded résumé claims, 12 firings were false ("Engineer, Contoso Freight", a summary sentence verbatim). | A short line (≤ 12 words) with no clause boundary, and a claim that adds a content word or turns a bare word into an -ed verb. The 4M fixtures still fire, and it adds 19 true catches ("Proficient in X", "Attended …"). |
| 3 | Role words (`routing._RX["role_or_team"]`) | "is the AGENT a person or a team?" | "a semaphore-regulated buffer managed by the OS layer" routed to role_or_team, so that claim was unverifiable in every run before the fix. | One condition: a role word followed by "by <determiner> <agent>" whose agent names no person or team is an artifact claim. |
| 4 | Refusal detector (`llm.detect_refusal`, FEEDBACK #14) | "is the model refusing THE REQUEST?" | "I cannot see which document triggered it yet", an agent reasoning aloud in a `thought` field, matched `first_person_cannot`. It raised LLMRefusalError and crashed a Phase 5 run. | Reasoning fields and long strings count only refusal-SHAPED language (refusal of the request, an opening apology, "as an AI"). |

**How to apply:**
- Before adding a word to a list, name the structural property the list approximates and test THAT. Examples: clause
  boundaries, sentence length, a construction, the agent of a passive, a field's role.
- A list of an OPEN class (verbs, nouns, job titles) will always run out. A list of a CLOSED class is acceptable, because
  it can be complete: negators, determiners, auxiliaries, guarantee quantifiers ("guarantee / never / always / fully"),
  -ing adjectives.
- **Still lexical, not changed yet:** M1 (`entries.reference_kind`, "is this entry line verbless?") still asks
  `verbs.has_verb`. It is the same question as M2, and the next candidate.
