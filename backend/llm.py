"""Central model router for Crucible.

Every model call in the project goes through ``call()``. Tasks map to a tier
(nano / super / ultra) and each tier has its own model id and base URL.
Nano lives on a different Token Factory endpoint than super and ultra, so a
single shared base URL does not work.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import openai
from openai import OpenAI

TASK_TIER = {
    # nano: cheap, high-volume
    "classify_hidden_text": "nano",
    "extract_claims": "nano",
    "classify_file": "nano",
    # super: working-tier reasoning and code
    "summarize_file": "super",
    "generate_tests": "super",
    "write_dockerfile": "super",
    "compose_bug": "super",
    "stranger_agent": "super",
    # ultra: verification and judgement
    "verify_claim": "ultra",
    "judge_rationale": "ultra",
    "compose_report": "ultra",
    "bias_check": "ultra",
}

# Casing of the ids is deliberately inconsistent and correct as written.
MODELS = {
    "nano": {
        "id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "base_url": "https://api.tokenfactory.nebius.com/v1/",
    },
    "super": {
        "id": "nvidia/nemotron-3-super-120b-a12b",
        "base_url": "https://api.tokenfactory.us-central1.nebius.com/v1/",
    },
    "ultra": {
        "id": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "base_url": "https://api.tokenfactory.us-central1.nebius.com/v1/",
    },
}

# USD per 1M tokens (input, output). nano is from the project brief. super is
# from third-party trackers (Maxim, TypingMind, Artificial Analysis), not yet
# confirmed on Nebius's own pricing page (checked 2026-10-09).
PRICING = {
    "nano": (0.06, 0.24),
    "super": (0.30, 0.90),
}

# Retry policy, applied by the OpenAI SDK on every client: up to MAX_RETRIES
# retries with exponential backoff on connection errors, timeouts, 408/409/429
# and 5xx. Without an explicit timeout the SDK default is 600 s.
REQUEST_TIMEOUT_S = 30.0
MAX_RETRIES = 2

# One JSON line per call (success or failure): task, tier, model, latency,
# tokens, cost. Never the prompt, the reply, or any credential.
MODEL_CALL_LOG = Path(__file__).resolve().parent.parent / "logs" / "model_calls.jsonl"
_log_lock = threading.Lock()

_clients = {}


class LLMError(Exception):
    pass


class LLMUnavailableError(LLMError):
    """The model cannot be reached at all: no key, bad key, or the network /
    endpoint failed after the retry policy. Callers may fall back offline."""


class LLMJSONError(LLMError):
    """The model reply could not be parsed as JSON."""

    def __init__(self, message, raw_text):
        super().__init__(message)
        self.raw_text = raw_text


def get_client(tier):
    """Return the cached OpenAI client for a tier, creating it on first use."""
    if tier not in MODELS:
        raise LLMError(f"Unknown tier: {tier!r}")
    if tier not in _clients:
        api_key = os.environ.get("NEBIUS_API_KEY")
        if not api_key:
            raise LLMUnavailableError("NEBIUS_API_KEY is not set")
        _clients[tier] = OpenAI(
            api_key=api_key,
            base_url=MODELS[tier]["base_url"],
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )
    return _clients[tier]


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def extract_json(text):
    """Parse JSON from a model reply.

    Models often wrap JSON in markdown code fences or prepend reasoning, so
    strip <think> blocks and fences, then fall back to the outermost
    object/array in the text.
    """
    cleaned = _THINK_RE.sub("", text or "").strip()
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMJSONError("Could not extract JSON from model reply", text)


def estimate_cost(tier, input_tokens, output_tokens):
    """USD cost for a call, or None if pricing for the tier is unknown."""
    if tier not in PRICING:
        return None
    in_rate, out_rate = PRICING[tier]
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _log_call(record):
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
    try:
        with _log_lock:
            MODEL_CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            with MODEL_CALL_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # logging must never break a model call


def call(task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None):
    """Run a task through its tier's model.

    ``schema`` may be a pydantic model class (reply is validated into it) or
    any other truthy value (reply is just parsed as JSON). ``system`` adds a
    system message. ``thinking=False`` disables Nemotron reasoning via
    ``chat_template_kwargs`` (the ``/no_think`` marker is ignored on Token
    Factory; see FEEDBACK.md). ``run_tag`` is copied into the call log.

    Returns a dict: text, data, task, tier, model, latency_s, input_tokens,
    output_tokens, cost_usd, finish_reason. ``data`` is None when no schema
    was given.

    Raises LLMUnavailableError when the model cannot be reached at all and
    LLMError for any other API failure.
    """
    if task not in TASK_TIER:
        raise LLMError(f"Unknown task: {task!r}")
    tier = TASK_TIER[task]
    model = MODELS[tier]["id"]

    messages = [{"role": "user", "content": prompt}]
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    extra = {}
    if thinking is not None:
        extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}

    base_log = {"task": task, "tier": tier, "model": model, "run_tag": run_tag}
    start = time.perf_counter()
    try:
        response = get_client(tier).chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **extra,
        )
    except (openai.APIConnectionError, openai.AuthenticationError, openai.PermissionDeniedError) as exc:
        _log_call({**base_log, "ok": False, "error": type(exc).__name__, "latency_s": time.perf_counter() - start})
        raise LLMUnavailableError(f"{tier} model unreachable: {type(exc).__name__}") from exc
    except LLMUnavailableError as exc:
        _log_call({**base_log, "ok": False, "error": "LLMUnavailableError", "latency_s": 0.0})
        raise
    except openai.APIError as exc:
        _log_call({**base_log, "ok": False, "error": type(exc).__name__, "latency_s": time.perf_counter() - start})
        raise LLMError(f"{tier} model call failed: {type(exc).__name__}") from exc
    latency_s = time.perf_counter() - start

    text = response.choices[0].message.content or ""
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    usage = response.usage
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    cost_usd = estimate_cost(tier, input_tokens, output_tokens)
    _log_call({
        **base_log, "ok": True, "latency_s": round(latency_s, 4), "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cost_usd": cost_usd, "finish_reason": finish_reason,
    })

    data = None
    if schema is not None:
        data = extract_json(text)
        if hasattr(schema, "model_validate"):
            data = schema.model_validate(data)

    return {
        "text": text,
        "data": data,
        "task": task,
        "tier": tier,
        "model": model,
        "latency_s": latency_s,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "finish_reason": finish_reason,
    }
