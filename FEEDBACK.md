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
