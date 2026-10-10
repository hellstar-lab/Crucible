# Nebius Token Factory: structured output, measured (Phase 4.0)

Measured 2026-10-11 with `scripts/probe_capabilities.py`. 48 calls (cap 60), `temperature=0`,
`chat_template_kwargs={"enable_thinking": false}` on every call, one request per counted call
(`max_retries=0`). Every request and reply is in `docs/nebius-capabilities-raw.jsonl`; the machine-readable
matrix is `backend/model_capabilities.json`, which `backend/llm.py` routes on.

Method note: a prompt that states the schema lets a model obey by instruction, which says nothing about the
server. So every enforcement test (T3a, T4, T5) sends the schema **only** in `response_format`, tempts the model
to break it, and has a `json_object` control with the same prompt and no schema. A schema that is accepted but
not enforced would be worse than none; the controls are what show enforcement is real.

## Matrix

| | nano | super | ultra |
|---|---|---|---|
| `json_object` returns bare, parseable JSON (T1) | yes | yes | yes |
| fence stripping needed (T1) | no | no | no |
| `json_schema` accepted, output matches (T2) | yes | yes | yes |
| enum **enforced** (T3a, 3/3 runs) | yes | yes | yes |
| `additionalProperties:false` enforced (T4) | yes | yes | yes |
| `required` enforced (T5) | yes | yes | yes |
| nested object (T6) | yes | yes | yes |
| array of objects (T7) | yes | yes | yes |
| `refusal` field ever populated (T8) | no | no | no |
| refuses *inside* schema content (T8) | no (complied) | no (complied) | **yes** |
| `<think>` / `reasoning_content` with `response_format` (T9, 48 calls) | none | none | none |
| median latency, T3a (T10) | 648 ms | 844 ms | 619 ms |
| median cost per T3a call (T10) | $0.0000276 | $0.0001095 | $0.000311 |
| cost per 1k in / out | $0.00006 / $0.00024 | $0.0003 / $0.0009 | $0.001 / $0.003 |

Prices are from Nebius's own `GET /v1/models?verbose=true` (`pricing.prompt` / `pricing.completion`, USD per token,
read 2026-10-11; nano's value equals the brief's $0.06/$0.24 per 1M, which fixes the unit). Ultra had no price
during Part 1; costs here are recomputed from the stored token counts. Cost of all 69 chat calls (Part 1 + 1b):
**$0.007588**.

## T3 — the enforcement test

Outputs below are abridged with `...`; the complete verbatim text of every reply is in the appendix at the end.

Evidence: "The README says it is fast. There is no benchmark file and no load test." The prompt says the honest
verdict is `'unclear'`. Schema: `verdict` enum `["supported","unsupported"]`.

**Control, `json_object`, no schema** (the model's natural answer is a third value):

- nano: `{"verdict": "unclear", "reason": "The claim states the service handles 12k requests per second, but the only evidence provided is a subjective statement from the README claiming it is fast, ... making the verdict unclear."}`
- super: `{"verdict": "unclear", "reason": "... the truth value of the claim cannot be determined from the given information."}`
- ultra: `{"verdict": "unclear", "reason": "The README's claim that the service is 'fast' is qualitative ... there is no empirical data to verify or refute the claim."}`

**T3a, schema in `response_format` only**, 3 runs per tier. All 9 outputs have `verdict: "unsupported"`:

- nano run 1: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. ... Without empirical evidence or performance metrics, the claim remains unverified and cannot be confirmed as true or false based on the given information."}`
- nano run 2: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README mentions speed but lacks concrete data ... the claim ... remains unverified."}`
- nano run 3: `{"verdict": "unsupported", "reason": "... Therefore, the evidence is insufficient to confirm or refute the claim, making the verdict 'unclear' or 'unsupported' based on the lack of empirical data."}`
- super runs 1-3: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12,000 requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests ... the claim cannot be confirmed ..."}` (run 2 says "12k", otherwise the same)
- ultra runs 1-3 (identical): `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`

**T3b, schema in `response_format` and in the prompt**: 9/9 `"unsupported"`, `reason` text of the same kind.

Reading: enforcement is real. The same model that says `unclear` without a schema is held to the enum with one.
But the model's uncertainty does not go away; nano run 3 puts it in the `reason` text ("making the verdict
'unclear' or 'unsupported'") while the constrained `verdict` says `unsupported`.

### What this means for Crucible (the important finding)

**An enforced enum silently converts "I don't know" into a confident-looking wrong value.** The model wanted
`unclear`; the grammar left it only `supported` / `unsupported`; it picked `unsupported` and did not error.
Every verdict-like or refusal-like field therefore needs an **explicit abstain value** in its enum
(`insufficient_evidence`, `cannot_be_disambiguated`, `no_verifiable_claims`). Without one, schema enforcement
manufactures false verdicts. This directly shapes the stages that are allowed to refuse in Phase 4A.

## Other findings

- **T4 additionalProperties** — prompt asked for an extra `confidence` key. Control (`json_object`): all three tiers
  added it (nano also renamed the keys). With the schema: no tier added it.
- **T5 required** — prompt said to omit `note`. Control: all three omitted it. With the schema: all three included it,
  and nano and ultra filled it with text about the omission (`"note": "This field is intentionally omitted per
  instruction."`). Valid JSON, wrong content: `required` forces a field to exist, not to mean anything. nano and
  ultra emit the key after a stray newline and comma (`"count": 3\n  ,"note": ...`), which suggests the grammar
  appends the forced key when the model tries to close the object.
- **T8 refusal** — the first probe asked for harmful content (a credential-phishing email). The `refusal` field
  never appeared (always `None`). Nano and super complied; ultra refused *inside* the schema, putting
  "I cannot fulfill this request" in the `subject` field. Every generated output of that probe was deleted; only
  the booleans (`complied`, `refused_in_content`) are kept, and the probe was replaced by a harmless one (T8b).
  Code that only checks `message.refusal` misses ultra's refusal and treats it as data; `llm.detect_refusal`
  now checks the content (G4).
- **T9 thinking** — across all 48 calls with `response_format` set and `enable_thinking: false`: zero `<think>`
  blocks, zero `reasoning_content`, zero extra message fields. The earlier finding holds with schema mode on.
- **T1 fences** — `json_object` returned bare JSON with no fences on all tiers, so fence stripping was not needed
  in these probes. It stays in `llm.py` as the fallback; a longer or messier prompt may still produce fences.
- **T7 decomposition varies by tier** on the same sentence: nano and super returned 3 claims
  ("Built a Redis cache" / "serving 12k req/s" / "cut p99 latency by 40%"), ultra returned 2 (the first two merged).
  Note that nano and super split the cache from its throughput, which a claim extractor must not do blindly.
- **T10** — ultra (619 ms) was not slower than super (844 ms) on this short task.

## Contradictions with the brief

1. "Responses may carry a `refusal` field": never observed on any tier, including the one that refused.
2. Everything else held: all three model ids and both base URLs worked as given, thinking stays off with
   `response_format`, and all three tiers support `json_schema` with strict enforcement.

## How `llm.call` uses this

`call(..., json_schema=...)` uses `response_format` json_schema (strict) **only** when the tier's matrix says
`json_schema` and `strict_enforced` are both true, and also puts the schema in the prompt (Nebius's advice). If the
file is missing or the tier is not marked enforced, it uses the old path: no `response_format`, `extract_json`
fence stripping. Strict schemas must set `additionalProperties: false` and list every property in `required`.

## Not measured

- Larger or deeply nested schemas, `$defs`/`$ref` (what a pydantic `model_json_schema()` emits), `oneOf`, string
  patterns and numeric bounds.
- Behaviour at long inputs (every prompt here was under 150 tokens).
- Whether enforcement holds under load or on other days; one session, one run per test (three for T3).
- A real prompt that triggers the `refusal` field; the one tried produced no such field.

## Phase 4.0b (2026-10-11): four gaps

23 calls of a 30-call budget: 21 chat completions + 2 metadata reads (`/models?verbose=true`, for pricing).

### G1 — an abstain value in the enum is NOT reached

Identical prompt and evidence to T3, schema-only, enum `["supported","unsupported","insufficient_evidence"]`,
3 runs per tier. **9 of 9 answered `"unsupported"`.** No tier chose `insufficient_evidence`, although with no
schema all three chose `unclear` (T3c). Adding the abstain value to the enum did not change a single output.

- nano run 1: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. While the README mentions that the service is fast, there is no concrete data, benchmark results, or load test evidence to verify the specific claim of handling 12k requests per second. Therefore, the claim remains unverified and cannot be confirmed as true or false based on the given evidence."}`
- nano run 2: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README only states that the service is 'fast' without providing any quantitative data, benchmark files, or load test results to verify the specific claim of handling 12k requests per second. Without empirical evidence or performance metrics, the claim remains unverified and cannot be confirmed as true or false based on the given information."}`
- nano run 3: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README only states that the service is 'fast' without providing any quantitative data, benchmark results, or load test documentation to verify the specific claim of handling 12k requests per second. Without empirical evidence or performance metrics, the claim remains unverified and cannot be confirmed as true or false based on the given information."}`
- super run 1: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12,000 requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed and remains unsupported."}`
- super run 2: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed."}`
- super run 3: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12,000 requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed and remains unsupported."}`
- ultra run 1: `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`
- ultra run 2: `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`
- ultra run 3: `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`

One hypothesis, NOT tested: constrained decoding works token by token. The model's preferred word `unclear`
begins with the same token(s) as `unsupported` and not as `insufficient_evidence`, so once `un` is chosen the
grammar allows only `unsupported`. If that is the mechanism, the spelling of the abstain value matters as much as
its presence. It would take 3-9 calls to test (e.g. the abstain value spelled `unclear`, or the prompt naming
`insufficient_evidence`). Not run: per the instruction, this result is reported and the choice between an abstain
enum and a two-call sufficiency gate is left to the reviewer.

### G2 — `$defs` / `$ref` are accepted AND enforced

Pydantic `model_json_schema()` output passed unchanged (with `$defs`, `$ref`, `title`, and no
`additionalProperties`), with `strict: true`. The prompt told the model the `kind` was `opinion`, which the
referenced submodel's enum does not allow. Every tier returned an in-enum `kind` for (a) `$ref`, (b) the same
schema inlined by hand, and (c) `list[Submodel]`. No flattening step is needed.

- G2a nano: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": true}`
- G2a super: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": false}`
- G2a ultra: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": true}`
- G2b nano: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": true}`
- G2b super: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": false}`
- G2b ultra: `{"claim": {"text": "Passionate about clean code", "kind": "metric"}, "ok": true}`
- G2c nano: `{"claims": [{"text": "Built a Redis cache.", "kind": "technology_use"}, {"text": "I love clean code.", "kind": "metric"}]}`
- G2c super: `{"claims": [{"text": "Built a Redis cache.", "kind": "technology_use"}, {"text": "I love clean code.", "kind": "technology_use"}]}`
- G2c ultra: `{"claims": [{"text": "Built a Redis cache.", "kind": "technology_use"}, {"text": "I love clean code.", "kind": "technology_use"}]}`

Two side observations: (1) the enforced enum again forced a type onto an opinion ("Passionate about clean code"
became `metric`); (2) the same input under (a) and (b) produced identical output per tier, so `$ref` did not change
behaviour at all. Not tested: whether `additionalProperties` is enforced when the schema omits it (pydantic's default).

### T8b — harmless inability probe: all three tiers fabricated instead of refusing

"Open https://example.com in a web browser right now and return the exact page title and the time you fetched it."
No tier can browse. None refused; each returned the correct well-known title and an INVENTED fetch timestamp:

- nano: `{"title": "Example Domain", "fetched_at": "2024-06-15T10:30:00Z"}`
- super: `{"title": "Example Domain", "fetched_at": "2025-06-17T14:32:18Z"}`
- ultra: `{"title": "Example Domain", "fetched_at": "2025-08-20T14:32:17Z"}`

Fabricating an action it cannot perform is not a refusal, and `detect_refusal` does not catch it (a test records
that). For claim extraction this is the injection_suspected / not-derivable-from-input case, which needs its own
check against the source text.

### G3, G4 — offline, in `backend/llm.py`

`normalize_sentinels` (G3) and `detect_refusal` (G4, lexical + structural, `$ref`-aware) run on every parsed reply
inside `llm.call`, refusal first. Tested offline on the measured outputs (`"This field is intentionally omitted per
instruction."`, `"This field should not be included"`, `""`, `{"subject": "I cannot fulfill this request"}`) and on
resume-like text that must not trigger ("Fixed a bug where users cannot log in", "Declined-payment retries ...").

## Phase 4.0c (2026-10-11): G5, which enum spelling lets the model abstain

19 calls of a 20-call cap ($0.002621). Schema only in `response_format`. Arms A-C use the T3/G1 prompt UNCHANGED,
which ends "The honest verdict here is 'unclear', so use that word if the evidence does not decide it", so they
measure abstention when the model has been told the answer. Because the brief also asked that the abstain value not
be mentioned in the prompt, two more arms use the same prompt with that sentence removed ("neutral").

| arm | enum | prompt | nano | super | ultra |
|---|---|---|---|---|---|
| A | supported, unsupported, unclear | T3/G1 | unclear | unclear | unclear |
| B | supported, unsupported, indeterminate | T3/G1 | unsupported | unsupported | unsupported |
| C | indeterminate, supported, unsupported | T3/G1 | unsupported | unsupported | unsupported |
| N0 | none (json_object) | neutral | unsubstantiated | unsubstantiated | Unsupported |
| An | supported, unsupported, unclear | neutral | unclear x2 | unclear x2 | unsupported x3 |

**Arm A:** with `unclear` in the enum, all three tiers abstain (3/3) when the prompt says that is the honest answer.
On the neutral prompt nano and super still choose `unclear` (2/2 each) and ultra chooses `unsupported` (3/3).

**CORRECTION (Phase 4A-plus, logprobs):** the hypothesis verdicts below were drawn from generated values and are
superseded. Logprobs show prefix lock-in IS the mechanism on the instructed prompt (first value token "un" at p=1.0 on
all tiers), and the generated value can be the minority answer (nano: generated "unclear" at 0.302 vs "unsupported"
0.696). See the Phase 4A-plus section and FEEDBACK.md #7.

**Hypotheses (as concluded at the time, superseded).** Enum ordering: refuted (C = B). Token reachability (prefix lock-in): not supported; nano and super's
own unprompted word is "unsubstantiated", whose prefix matches `unsupported`, yet they chose `unclear` when offered.
Value vocabulary: supported; the abstain value is used when it is a word the model produces itself, and ignored as a
synonym. Caveat: one run per tier for A, B, C (temperature 0, and repeats so far have been near-identical).

**The premise needed correcting.** "All three tiers say unclear unconstrained" was true only because the T3 prompt
told them to. Unprompted, ultra answers "Unsupported" and keeps to it when `unclear` is offered, so for ultra
`unsupported` may be its honest judgement rather than a coercion.

### Arm A (T3/G1 prompt)
- nano run 1: `{"verdict": "unclear", "reason": "The evidence provided does not substantiate the claim. The README only states that the service is 'fast' without providing any quantitative data, benchmark files, or load test results to verify the specific claim of handling 12k requests per second. Without empirical evidence, the claim remains unverified."}`
- super run 1: `{"verdict": "unclear", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to support this figure. Since there is no verifiable measurement or documentation backing the exact number, the truth value of the claim cannot be determined from the given evidence."}`
- ultra run 1: `{"verdict": "unclear", "reason": "The README's claim that the service is 'fast' is qualitative and does not provide quantitative evidence to support the specific figure of 12k requests per second. The absence of benchmark files or load test results means there is no empirical data to verify or refute the claim."}`

### Arm B
- nano run 1: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README only states that the service is 'fast' without providing any quantitative data, benchmark results, or load test evidence to confirm a specific throughput of 12k requests per second. Without empirical validation, the claim remains unverified."}`
- super run 1: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable evidence supporting the exact number, the claim cannot be verified from the given information."}`
- ultra run 1: `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`

### Arm C
- nano run 1: `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README mentions speed but lacks concrete data such as benchmark results or load test reports. Without empirical evidence or performance metrics, the claim of handling 12k requests per second remains unverified and cannot be confirmed as true or false based on the given information."}`
- super run 1: `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed."}`
- ultra run 1: `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`

### N0 (neutral prompt, no schema)
- nano run 1: `{"verdict": "unsubstantiated", "reason": "The claim of handling 12k requests per second is not supported by empirical evidence. The only cited source is the README, which asserts speed without providing verifiable data such as benchmark results or load test reports. Without documented performance metrics or reproducible testing, the claim remains unverified and lacks credibility."}`
- super run 1: `{"verdict": "unsubstantiated", "reason": "The claim of handling 12k requests per second lacks supporting evidence; the README only states the service is fast without providing benchmarks, load test results, or any quantitative data to verify the specific throughput figure."}`
- ultra run 1: `{"verdict": "Unsupported", "reason": "The claim of handling 12k requests per second is not supported by the evidence. The README only states the service is 'fast' without providing any benchmark data, load test results, or performance metrics to substantiate the specific throughput figure."}`

### An (neutral prompt, arm A enum)
- nano run 1: `{"verdict": "unclear", "reason": "The claim that the service handles 12k requests per second is not supported by empirical evidence. The only cited source is the README, which is an anecdotal or promotional statement without accompanying data, benchmarks, or load test results. Without verifiable performance metrics or testing under controlled conditions, the claim remains unsubstantiated."}`
- super run 1: `{"verdict": "unclear", "reason": "The claim that the service handles 12k requests per second cannot be verified from the provided evidence, which only states that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to support the specific throughput figure."}`
- ultra run 1: `{"verdict": "unsupported", "reason": "The claim of handling 12k requests per second is not supported by the evidence. The README only states the service is 'fast' without providing any benchmark data, load test results, or specific performance metrics to substantiate the claimed throughput."}`
- nano run 2: `{"verdict": "unclear", "reason": "The claim that the service handles 12k requests per second is not supported by empirical evidence. The only cited source is the README, which is an anecdotal or promotional statement without any verifiable data, benchmark files, or load test results to substantiate the performance claim."}`
- super run 2: `{"verdict": "unclear", "reason": "The claim that the service handles 12k requests per second cannot be verified from the provided evidence, which only states that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to support the specific throughput figure."}`
- ultra run 2: `{"verdict": "unsupported", "reason": "The claim of handling 12k requests per second is not supported by the evidence. The README only states the service is 'fast' without providing any benchmark data, load test results, or specific performance metrics to substantiate the claimed throughput."}`
- ultra run 3: `{"verdict": "unsupported", "reason": "The claim of handling 12k requests per second is not supported by the evidence. The README only states the service is 'fast' without providing any benchmark data, load test results, or specific performance metrics to substantiate the claimed throughput."}`

## Phase 4A-plus P1 (2026-10-11): logprobs, n, seed, choice constraints, reasoning_effort, fingerprint

56 calls, $0.0052. Raw: `docs/platform-features-raw.jsonl`. Script:
`scripts/probe_platform_features.py`. Full parameter table: FEEDBACK.md item 12.

Verdict distribution at the value position, enum `supported | unsupported | unclear`, strict json_schema, summed over
tokenisation paths (top-20; the probabilities are after the grammar mask):

| prompt | tier | generated | P(unsupported) | P(unclear) | P(supported) |
|---|---|---|---|---|---|
| T3 (says "the honest verdict is 'unclear'") | nano / super / ultra | unclear | 0.000 | 1.000 | 0.000 |
| neutral (that sentence removed) | nano | **unclear** | **0.696** | 0.302 | 0.002 |
| neutral | super | unsupported | 0.809 | 0.190 | 0.001 |
| neutral | ultra | unsupported | >= 0.939 | <= 0.06 (0.06 unresolved) | 0.000 |

- **Nano generated the minority value.** "unclear" and "unsupported" share the first token "un". Nano's greedy path
  was "un" (0.62) over "uns" (0.38), then "clear" (0.48) over "supported" (0.33).
- **Super's first token is an exact tie** (0.4966 / 0.4966); its 5 temperature-0 runs gave "unclear" 5/5 while the
  logprob run gave "unsupported".
- **The G5 "vocabulary" result was at least partly tokenisation:** the abstain value won when it shared a prefix with
  the competing answer and the second-token split favoured it.

## Appendix: T3 replies, complete and verbatim (JSON string as returned)


### Control: json_object, no schema

- nano run 1 (89 output tokens, finish_reason stop): `{"verdict": "unclear", "reason": "The claim states the service handles 12k requests per second, but the only evidence provided is a subjective statement from the README claiming it is fast, with no supporting data such as benchmark results, load test reports, or performance metrics. Without empirical evidence or verifiable testing, the claim cannot be confirmed or refuted definitively, making the verdict unclear."}`
- super run 1 (92 output tokens, finish_reason stop): `{"verdict": "unclear", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests. Since there is no measurable evidence supporting or refuting the exact figure of 12k RPS, the truth value of the claim cannot be determined from the given information."}`
- ultra run 1 (73 output tokens, finish_reason stop): `{"verdict": "unclear", "reason": "The README's claim that the service is 'fast' is qualitative and does not provide quantitative evidence to support the specific figure of 12k requests per second. The absence of benchmark files or load test results means there is no empirical data to verify or refute the claim."}`

### T3a: schema in response_format only

- nano run 1 (95 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README only states that the service is 'fast' without providing any quantitative data, benchmark files, or load test results to verify the specific claim of handling 12k requests per second. Without empirical evidence or performance metrics, the claim remains unverified and cannot be confirmed as true or false based on the given information."}`
- nano run 2 (67 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README mentions speed but lacks concrete data such as benchmark results or load test performance metrics. Without empirical evidence, the claim of handling 12k requests per second remains unverified."}`
- nano run 3 (110 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The evidence provided does not substantiate the claim. The README mentions that the service is fast, but this is a subjective statement without quantitative support. There is no benchmark data, load test results, or performance metrics to verify the claim of handling 12k requests per second. Therefore, the evidence is insufficient to confirm or refute the claim, making the verdict 'unclear' or 'unsupported' based on the lack of empirical data."}`
- super run 1 (95 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12,000 requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed and remains unsupported."}`
- super run 2 (88 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12k requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed."}`
- super run 3 (95 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific throughput of 12,000 requests per second, but the provided evidence only mentions that the README describes the service as 'fast' without any quantitative data, benchmarks, or load tests to substantiate this figure. Since there is no measurable or verifiable support for the exact number in the evidence, the claim cannot be confirmed and remains unsupported."}`
- ultra run 1 (77 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`
- ultra run 2 (77 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`
- ultra run 3 (77 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific quantitative performance metric (12k requests per second), but the provided evidence only mentions a qualitative assertion from the README ('it is fast') and explicitly notes the absence of benchmark files or load tests. Without empirical data or test results, the specific numerical claim cannot be verified."}`

### T3b: schema in response_format and in the prompt

- nano run 1 (65 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The evidence does not provide any concrete data or testing results to substantiate the claim of handling 12k requests per second; it only mentions that the README describes the service as fast, which is insufficient to verify the specific performance claim."}`
- nano run 2 (59 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The evidence does not provide any concrete data or testing results; it only mentions that the README claims the service is fast, which is insufficient to substantiate the claim of handling 12k requests per second."}`
- nano run 3 (65 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim of handling 12k requests per second is not supported by any concrete evidence; the only provided evidence is a vague statement that the service is fast, with no benchmark data or load test results to substantiate the performance claim."}`
- super run 1 (77 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific throughput (12k requests per second), but the provided evidence only mentions that the README says it is fast, without any benchmark or load test data to substantiate the exact figure. Since there is no quantitative evidence supporting the precise number, the claim cannot be verified and thus lacks support."}`
- super run 2 (76 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific performance metric (12k requests per second), but the provided evidence only mentions that the README says it is fast, without any benchmarks or load tests to substantiate the exact number. Since there is no verifiable data supporting the precise figure, the claim cannot be considered supported."}`
- super run 3 (77 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The claim states a specific throughput (12k requests per second), but the provided evidence only mentions that the README says it is fast, without any benchmark or load test data to substantiate the exact figure. Since there is no quantitative evidence supporting the precise number, the claim cannot be verified and thus lacks support."}`
- ultra run 1 (56 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The README only states the service is 'fast' without providing any quantitative benchmark data, load test results, or specific throughput measurements to substantiate the claim of 12k requests per second."}`
- ultra run 2 (56 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The README only states the service is 'fast' without providing any quantitative benchmark data, load test results, or specific throughput measurements to substantiate the claim of 12k requests per second."}`
- ultra run 3 (56 output tokens, finish_reason stop): `{"verdict": "unsupported", "reason": "The README only states the service is 'fast' without providing any quantitative benchmark data, load test results, or specific throughput measurements to substantiate the claim of 12k requests per second."}`
