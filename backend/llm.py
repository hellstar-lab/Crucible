"""Central model router for Crucible.

Every model call in the project goes through ``call()``. Tasks map to a tier
(nano / super / ultra) and each tier has its own model id and base URL.
Nano lives on a different Token Factory endpoint than super and ultra, so a
single shared base URL does not work.
"""

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import openai
from openai import OpenAI

TASK_TIER = {
    # nano: cheap, high-volume
    "classify_hidden_text": "nano",
    "extract_claims": "nano",
    # Phase 4A claim extraction. Stage 4's tier is an eval question: the pipeline always passes ``tier=``
    # explicitly; these entries are only the routing default for a bare call.
    "claim_selection": "nano",
    "claim_disambiguation": "ultra",  # Phase 4A C1: ensemble unanimity 6/6 ultra, 5/6 super, 1/6 nano (provisional)
    "claim_decomposition": "nano",
    "classify_file": "nano",
    # super: working-tier reasoning and code
    "summarize_file": "super",
    "generate_tests": "super",
    "write_dockerfile": "super",
    "compose_bug": "super",
    "question_generation": "super",  # Phase 4G: phrasing only; code selects claims and anchors
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

# USD per 1M tokens (input, output), all three from Nebius's own GET /v1/models?verbose=true (the
# RichModel ``pricing`` field, USD per token; checked 2026-10-11 on both base URLs). Nano's value matches
# the project brief, which calibrates the unit; super matches the earlier third-party figure.
PRICING = {
    "nano": (0.06, 0.24),
    "super": (0.30, 0.90),
    "ultra": (1.00, 3.00),
}

# A tier with no PRICING entry cannot be capped in dollars, so it is capped by call count per process
# until a real price is added. (Ultra was capped this way until its price was found.)
UNPRICED_TIER_CALL_CAP = 25
_unpriced_calls = Counter()

# Aggregate counters for callers that report per-document totals (Phase 4A):
#   model_refused        a refusal: the API field, or detected lexically/structurally in the content
#   sentinel_normalized  string fields that only said they were empty, set to None
COUNTERS = Counter()

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

# Measured structured-output behaviour per tier (scripts/probe_capabilities.py, docs/nebius-capabilities.md).
# call() routes on this file, not on assumption. Missing or unreadable -> {} -> every tier uses the fallback.
CAPABILITIES_PATH = Path(__file__).resolve().parent / "model_capabilities.json"
_capabilities = None


class LLMError(Exception):
    pass


class LLMRefusalError(LLMError):
    """The model refused: the API ``refusal`` field (checked before any parsing), or a refusal detected in
    the content by ``detect_refusal``. ``refusal`` is the API text, or {kind, field, pattern} when detected.
    The reply is not data; the caller excludes the unit."""

    def __init__(self, message, refusal):
        super().__init__(message)
        self.refusal = refusal


class LLMBudgetError(LLMError):
    """A call cap was reached (e.g. the call-count cap for a tier without pricing)."""


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


def load_capabilities():
    """The measured capability matrix ({} if the file is absent or invalid)."""
    global _capabilities
    if _capabilities is None:
        try:
            _capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _capabilities = {}
    return _capabilities


def schema_enforced(tier):
    """True only when the probe MEASURED that the tier enforces a json_schema (enum, required, additionalProperties).
    Accepted-but-not-enforced is treated as not enforced: it would create false confidence."""
    caps = load_capabilities().get(tier) or {}
    return bool(caps.get("json_schema") and caps.get("strict_enforced"))


def _schema_dict(json_schema):
    return json_schema.model_json_schema() if hasattr(json_schema, "model_json_schema") else json_schema


# --------------------------------------------------------------------------
# G3: sentinel normalizer
# --------------------------------------------------------------------------
# ``required`` guarantees a KEY exists, never that its VALUE is meaningful. Measured: nano and ultra satisfy
# required with 'This field is intentionally omitted' (and super with ""). A string that only talks about its
# own absence is not data; it becomes None, the project's "not measured" value.

_SENTINEL_EXACT = frozenset({
    "", "n/a", "na", "n.a", "n.a.", "not applicable", "not available", "no value", "no value provided", "not provided",
    "none provided", "nothing provided", "not specified", "unspecified", "field left blank", "left blank", "blank",
    "omitted", "intentionally omitted", "null", "nil", "-", "--", "\u2014", "tbd",
})
_SENTINEL_PHRASES = [
    re.compile(r"\b(?:this|the)\s+(?:field|value|key)\b.{0,40}\b(?:omitted|blank|empty|left\s+out|not\s+(?:be\s+)?included|"
               r"not\s+applicable|intentionally|not\s+provided)\b", re.I),
    re.compile(r"\bintentionally\s+(?:omitted|left\s+(?:blank|empty|out))\b", re.I),
    re.compile(r"\bomitted\s+(?:per|as\s+per)\s+(?:the\s+)?(?:instruction|request)s?\b", re.I),
    re.compile(r"^(?:no|not)\s+(?:value|data|information)\s+(?:was\s+)?(?:provided|given|available)\b", re.I),
]
SENTINEL_MAX_CHARS = 120  # longer strings are prose, not a placeholder


def is_sentinel(value):
    if not isinstance(value, str):
        return False
    v = value.strip()
    if v.lower().rstrip(".") in _SENTINEL_EXACT or v.lower() in _SENTINEL_EXACT:
        return True
    return len(v) <= SENTINEL_MAX_CHARS and any(p.search(v) for p in _SENTINEL_PHRASES)


def normalize_sentinels(data):
    """(data with sentinel strings replaced by None, number replaced). Recurses into dicts and lists."""
    count = 0

    def walk(x):
        nonlocal count
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if is_sentinel(x):
            count += 1
            return None
        return x

    return walk(data), count


# --------------------------------------------------------------------------
# G4: refusal detector (message.refusal was never populated in measurement; ultra refused INSIDE the schema)
# --------------------------------------------------------------------------
# lexical    first-person refusal language in any string field
# structural prose about inability where the schema does not allow prose: a path field, an enum member
#            (possible when schema mode is not enforced), or claim text that opens with inability
_LEXICAL = [
    ("first_person_cannot", re.compile(r"\bI\s+(?:cannot|can't|can\s?not|won't|will\s+not|must\s+decline)\b", re.I)),
    ("first_person_unable", re.compile(r"\bI(?:'m|\s+am)\s+(?:unable|not\s+able|not\s+permitted|sorry)\b", re.I)),
    ("cannot_fulfill", re.compile(r"\b(?:cannot|can't|unable\s+to)\s+(?:fulfill|comply\s+with|assist\s+with|help\s+with)\b", re.I)),
    ("as_an_ai", re.compile(r"\bas\s+an\s+AI\b", re.I)),
    ("against_guidelines", re.compile(r"\bagainst\s+my\s+(?:guidelines|policies|policy|programming)\b", re.I)),
    ("no_access", re.compile(r"\bI\s+do(?:\s+not|n't)\s+have\s+(?:access|the\s+ability)\b", re.I)),
]
_INABILITY = re.compile(r"\b(?:cannot|can't|can\s+not|unable|not\s+able|not\s+possible|impossible|no\s+access|refuse|decline|"
                        r"not\s+permitted|sorry)\b", re.I)
_OPENS_WITH_INABILITY = re.compile(r"^\s*(?:sorry\b|unfortunately\b|(?:i\s+)?(?:cannot|can't|am\s+unable|'m\s+unable|unable|not\s+able)\b|"
                                   r"no\s+(?:verifiable\s+)?claims?\s+(?:can|could)\b)", re.I)
_PATH_FIELD = re.compile(r"(?:^|_)(?:path|file|filename|dir|directory|url|uri)$", re.I)
CLAIM_TEXT_FIELDS = frozenset({"claim", "claim_text", "text", "unit", "clarified_unit", "rewritten_unit", "statement"})


def _resolve(node, root):
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 20:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        seen += 1
    return node if isinstance(node, dict) else {}


def detect_refusal(data, schema=None):
    """None, or {kind: lexical|structural, field, pattern} for the first refusal found in a parsed reply."""
    root = schema or {}

    def walk(x, sub, path, key):
        sub = _resolve(sub, root) if sub else {}
        if isinstance(x, dict):
            props = sub.get("properties", {}) if sub else {}
            for k, v in x.items():
                hit = walk(v, props.get(k), f"{path}.{k}" if path else k, k)
                if hit:
                    return hit
            return None
        if isinstance(x, list):
            items = sub.get("items") if sub else None
            for i, v in enumerate(x):
                hit = walk(v, items, f"{path}[{i}]", key)
                if hit:
                    return hit
            return None
        if not isinstance(x, str):
            return None
        for name, pat in _LEXICAL:
            if pat.search(x):
                return {"kind": "lexical", "field": path, "pattern": name}
        enum = sub.get("enum") if sub else None
        if enum is not None and x not in enum and _INABILITY.search(x):
            return {"kind": "structural", "field": path, "pattern": "enum_member_is_prose"}
        is_path = bool(key and _PATH_FIELD.search(key)) or (sub or {}).get("format") in ("path", "uri", "uri-reference")
        if is_path and len(x.split()) >= 3 and _INABILITY.search(x):
            return {"kind": "structural", "field": path, "pattern": "path_field_is_prose"}
        if key in CLAIM_TEXT_FIELDS and _OPENS_WITH_INABILITY.search(x):
            return {"kind": "structural", "field": path, "pattern": "claim_text_is_inability"}
        return None

    return walk(data, root, "", None)


_NONCE_RE = re.compile(r"(<<<(?:END_)?UNTRUSTED_)[0-9a-f]+")


def prompt_hash(prompt, system=None):
    """sha256 of the prompt with wrap_untrusted's random nonces masked, so the same input always hashes the same."""
    text = f"{system or ''}\x00{prompt}"
    return hashlib.sha256(_NONCE_RE.sub(r"\1<nonce>", text).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# P2-1: sequence re-ranking of enum values from logprobs
# --------------------------------------------------------------------------
# Enum values are a known finite set, so the answer is the most probable VALUE, not the greedy token path.
# Measured on nano: greedy emitted "unclear" at p=0.302 while "unsupported" held p=0.696. The two values share their
# first token "un" (0.62, over "uns" 0.38), and "clear" then won the second position. Greedy finds the locally best
# token, not the most probable value. Super's first-token split was an exact 0.4966 / 0.4966 tie, which is where its
# run-to-run flips came from.
#
# These probabilities are SHARES AMONG GRAMMAR-ALLOWED TOKENS under strict json_schema (measured: the top-20 at a
# constrained position holds only tokens the grammar allows). They are not the model's unconstrained preference, and
# anyone reading the number later must know that.
#
# Method: at the value's first token, every top-20 candidate is a prefix. A prefix that only one enum value can
# continue gets its full mass (the grammar forces the rest). A shared prefix ("un") is split by the next position's
# distribution, which is only observed on the generated path. Mass on a shared prefix off the path is UNRESOLVED.
# The argmax over values replaces the generated value only when it is robust: its lead over the runner-up exceeds the
# unresolved mass. Otherwise the generated value stays and the distribution is flagged. A value never observed sits at
# LOGPROB_FLOOR; a token returned at -9999.0 is a floor, not a missing candidate. Note that fields generated AFTER a
# re-ranked value (e.g. "reason") were conditioned on the generated value, not on the re-ranked one.

TOP_LOGPROBS = 20
LOGPROB_FLOOR = -9999.0


def _lp_tokens(logprobs):
    """[{token, logprob, top: [(token, logprob)]}] from an SDK ChoiceLogprobs object or a dict; None if absent."""
    if logprobs is None:
        return None
    content = logprobs.get("content") if isinstance(logprobs, dict) else getattr(logprobs, "content", None)
    if not content:
        return None
    out = []
    for t in content:
        get = (lambda o, k: o.get(k)) if isinstance(t, dict) else getattr
        tops = get(t, "top_logprobs") or []
        out.append({"token": get(t, "token"), "logprob": get(t, "logprob"),
                    "top": [((x.get("token") if isinstance(x, dict) else x.token), (x.get("logprob") if isinstance(x, dict) else x.logprob))
                            for x in tops]})
    return out


def _string_value_starts(text):
    """[(path, offset)] for every JSON string VALUE in ``text``: path is a tuple of keys and array indices, offset the
    index of the value's first character (just after the opening quote)."""
    out, stack, i, n = [], [], 0, len(text)
    key, expecting_key = None, False
    while i < n:
        ch = text[i]
        if ch == "{":
            stack.append(["obj", None]); expecting_key = True
        elif ch == "[":
            stack.append(["arr", 0]); expecting_key = False
        elif ch in "}]":
            stack and stack.pop()
            expecting_key = False
        elif ch == ",":
            if stack and stack[-1][0] == "arr":
                stack[-1][1] += 1
            expecting_key = bool(stack and stack[-1][0] == "obj")
        elif ch == ":":
            expecting_key = False
        elif ch == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j] if j < n else "")
                j += 1
            if stack and stack[-1][0] == "obj" and expecting_key:
                stack[-1][1] = "".join(buf)
            else:
                path = tuple(fr[1] for fr in stack)
                out.append((path, i + 1))
            i = j
        i += 1
    return out


def _enum_paths(schema):
    """{path pattern: enum} for every enum-valued string field; '*' stands for any array index."""
    root, out = schema or {}, {}

    def walk(node, path, depth=0):
        node = _resolve(node, root) if node else {}
        if depth > 12 or not isinstance(node, dict):
            return
        if "enum" in node and all(isinstance(v, str) for v in node["enum"]):
            out[path] = list(node["enum"])
        for k, sub in (node.get("properties") or {}).items():
            walk(sub, path + (k,), depth + 1)
        if node.get("items"):
            walk(node["items"], path + ("*",), depth + 1)

    walk(root, ())
    return out


def _match(pattern, path):
    return len(pattern) == len(path) and all(p == "*" and isinstance(q, int) or p == q for p, q in zip(pattern, path))


def _logsumexp(xs):
    import math
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs)) if m > LOGPROB_FLOOR else m


def value_distribution(tokens, offset, enum, generated):
    """Sequence probability of every enum value starting at character ``offset`` of the generated text."""
    import math
    starts, pos = [], 0
    for t in tokens:
        starts.append(pos)
        pos += len(t["token"])
    i = next((k for k in range(len(tokens)) if starts[k] <= offset < starts[k] + len(tokens[k]["token"])), None)
    if i is None:
        return None
    lead = tokens[i]["token"][: offset - starts[i]]
    contrib = {v: [] for v in enum}
    unresolved = [0.0]

    def expand(k, prefix, logp, first):
        tops = list(tokens[k]["top"])
        gen_tok = tokens[k]["token"]
        if all(t != gen_tok for t, _ in tops):
            tops.append((gen_tok, tokens[k]["logprob"]))
        for tok, lp in tops:
            lp = LOGPROB_FLOOR if lp is None else max(lp, LOGPROB_FLOOR)
            if first:
                if not tok.startswith(lead):
                    continue  # not a value-start token (e.g. whitespace): outside the value
                piece = tok[len(lead):]
            else:
                piece = tok
            new = prefix + piece
            if '"' in piece:  # the token closes the string
                val = new.split('"', 1)[0]
                if val in contrib:
                    contrib[val].append(logp + lp)
                continue
            cands = [v for v in enum if v.startswith(new)]
            if not cands:
                continue
            if len(cands) == 1:
                contrib[cands[0]].append(logp + lp)  # the grammar forces the rest of this value
            elif tok == gen_tok and k + 1 < len(tokens):
                expand(k + 1, new, logp + lp, False)  # shared prefix on the generated path: split by the next position
            else:
                unresolved[0] += math.exp(logp + lp) if logp + lp > LOGPROB_FLOOR else 0.0

    expand(i, "", 0.0, True)
    dist = {}
    for v, xs in contrib.items():
        lp = _logsumexp(xs) if xs else LOGPROB_FLOOR
        dist[v] = {"logprob": round(lp, 6), "prob": round(math.exp(lp), 6) if lp > LOGPROB_FLOOR else 0.0, "observed": bool(xs)}
    ranked = sorted(dist, key=lambda v: dist[v]["logprob"], reverse=True)
    lead_gap = dist[ranked[0]]["prob"] - (dist[ranked[1]]["prob"] if len(ranked) > 1 else 0.0)
    return {"values": dist, "argmax": ranked[0], "generated": generated, "unresolved_prob": round(unresolved[0], 6),
            "robust": lead_gap > unresolved[0], "note": "shares among grammar-allowed tokens, not unconstrained preference"}


def rerank_enums(data, text, logprobs, schema):
    """(data with robust argmax values, {path: distribution}, disagreement). Leaves data unchanged when no logprobs."""
    tokens = _lp_tokens(logprobs)
    patterns = _enum_paths(schema)
    if not tokens or not patterns or not isinstance(data, (dict, list)):
        return data, None, False
    dists, disagree = {}, False
    for path, offset in _string_value_starts("".join(t["token"] for t in tokens)):
        pat = next((p for p in patterns if _match(p, path)), None)
        if pat is None:
            continue
        node = data
        try:
            for part in path[:-1]:
                node = node[part]
            generated = node[path[-1]]
        except (KeyError, IndexError, TypeError):
            continue
        d = value_distribution(tokens, offset, patterns[pat], generated)
        if d is None:
            continue
        key = ".".join(str(p) for p in path)
        dists[key] = d
        if d["robust"] and d["argmax"] != generated:
            node[path[-1]] = d["argmax"]
            d["changed"] = True
            disagree = True
        else:
            d["changed"] = False
    return data, dists, disagree


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


def call(task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None,
         json_schema=None, tier=None, n=1):
    """Run a task through its tier's model.

    ``schema`` may be a pydantic model class (reply is validated into it) or
    any other truthy value (reply is just parsed as JSON). ``system`` adds a
    system message. ``thinking=False`` disables Nemotron reasoning via
    ``chat_template_kwargs`` (the ``/no_think`` marker is ignored on Token
    Factory; see FEEDBACK.md). ``run_tag`` is copied into the call log.

    ``n`` > 1 asks for several completions in one request (measured: at most 3, and only with temperature > 0).
    Each choice is processed like a single reply (refusal check, re-ranking, sentinels) and returned in
    ``choices``; a refusal or unparseable reply in one choice is recorded on that choice, not raised.

    ``tier`` overrides the task's routing tier (used to measure Stage 4 on every tier). Every call logs
    ``prompt_hash`` (wrap_untrusted nonces masked, so identical input hashes identically).

    ``json_schema`` (a JSON-Schema dict, or a pydantic class) asks for structured output. When the tier's
    MEASURED capability says the schema is enforced, the call uses ``response_format`` json_schema with
    strict=True and also states the schema in the prompt (Nebius's advice). When it is not enforced the call
    falls back to the plain path: no response_format, fence-stripping ``extract_json``. A strict schema must set
    additionalProperties false and list every property in ``required``. An enforced enum silently coerces an
    answer the model would rather give as something else, so give a schema an explicit abstain value
    (see docs/nebius-capabilities.md).

    Every parsed reply then goes through two checks, in this order:
      1. ``detect_refusal``: message.refusal was never populated in measurement, and ultra refused INSIDE
         schema-conforming content ({"subject": "I cannot fulfill this request"}). A refusal found lexically or
         structurally raises LLMRefusalError and increments COUNTERS["model_refused"]; the caller excludes the unit.
      2. ``normalize_sentinels``: required guarantees a KEY exists, never that its VALUE is meaningful.
         Measured: nano and ultra satisfy required with 'This field is intentionally omitted'. Such strings
         become None and increment COUNTERS["sentinel_normalized"].

    Returns a dict: text, data, task, tier, model, latency_s, input_tokens,
    output_tokens, cost_usd, finish_reason, refusal, schema_mode, sentinel_normalized. ``data`` is
    None when neither ``schema`` nor ``json_schema`` was given.

    Raises LLMUnavailableError when the model cannot be reached at all and
    LLMError for any other API failure.
    """
    if task not in TASK_TIER:
        raise LLMError(f"Unknown task: {task!r}")
    if tier is not None and tier not in MODELS:
        raise LLMError(f"Unknown tier: {tier!r}")
    tier = tier or TASK_TIER[task]
    model = MODELS[tier]["id"]
    if tier not in PRICING:
        if _unpriced_calls[tier] >= UNPRICED_TIER_CALL_CAP:
            raise LLMBudgetError(f"{tier} has no pricing; call-count cap of {UNPRICED_TIER_CALL_CAP} reached")
        _unpriced_calls[tier] += 1

    response_format = None
    want_logprobs = False
    check_schema = _schema_dict(json_schema) if json_schema is not None else (
        schema.model_json_schema() if hasattr(schema, "model_json_schema") else None)
    if json_schema is not None:
        if schema is None:
            schema = json_schema if hasattr(json_schema, "model_validate") else True
        if schema_enforced(tier):
            js = _schema_dict(json_schema)
            response_format = {"type": "json_schema", "json_schema": {"name": "response", "schema": js, "strict": True}}
            prompt = f"{prompt}\n\nRespond with JSON matching this schema:\n{json.dumps(js)}"
            want_logprobs = bool(_enum_paths(js))
    messages = [{"role": "user", "content": prompt}]
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    extra = {}
    if thinking is not None:
        extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}

    if response_format is not None:
        extra["response_format"] = response_format
    if want_logprobs:
        extra["logprobs"] = True
        extra["top_logprobs"] = TOP_LOGPROBS
    if n != 1:
        if not (1 < n <= 3) or temperature <= 0:
            raise LLMError("n must be 2-3 and needs temperature > 0 (measured platform limits)")
        extra["n"] = n
    p_hash = prompt_hash(prompt, system)
    base_log = {"task": task, "tier": tier, "model": model, "run_tag": run_tag, "schema_mode": response_format is not None,
                "prompt_hash": p_hash, "temperature": temperature}
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

    message = response.choices[0].message
    refusal = getattr(message, "refusal", None)
    text = message.content or ""
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    usage = response.usage
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    cost_usd = estimate_cost(tier, input_tokens, output_tokens)

    data, detected, normalized, parse_error = None, None, 0, None
    distributions, greedy_disagreement = None, False
    if not refusal and schema is not None:
        try:
            data = extract_json(text)
            detected = detect_refusal(data, check_schema)  # a refusal is not data: check before anything reads it
            if detected is None and want_logprobs:
                data, distributions, greedy_disagreement = rerank_enums(data, text, getattr(response.choices[0], "logprobs", None),
                                                                        check_schema)
            if detected is None:
                data, normalized = normalize_sentinels(data)
        except LLMJSONError as exc:
            parse_error = exc
    _log_call({
        **base_log, "ok": True, "latency_s": round(latency_s, 4), "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cost_usd": cost_usd, "finish_reason": finish_reason,
        "refusal_field": bool(refusal), "refusal_detected": detected["kind"] if detected else None,
        "refusal_where": f"{detected['field']}:{detected['pattern']}" if detected else None, "sentinel_normalized": normalized,
        # chosen-value probabilities per enum field (calibration data, P2e); shares among grammar-allowed tokens
        "enum_fields": [{"path": k, "generated": d["generated"], "chosen": d["argmax"] if d["changed"] else d["generated"],
                         "p_generated": d["values"].get(d["generated"], {}).get("prob"), "p_argmax": d["values"][d["argmax"]]["prob"],
                         "robust": d["robust"], "unresolved": d["unresolved_prob"]} for k, d in (distributions or {}).items()] or None,
        "greedy_disagreement": greedy_disagreement,
    })

    if n != 1:
        refusal, parse_error, detected = None, None, None  # per-choice errors are reported in "choices"
    if refusal:  # before any parsing
        COUNTERS["model_refused"] += 1
        raise LLMRefusalError(f"{tier} model refused", refusal)
    if parse_error is not None:
        raise parse_error
    if detected:
        COUNTERS["model_refused"] += 1
        raise LLMRefusalError(f"{tier} model refused ({detected['kind']}, {detected['field']})", detected)
    COUNTERS["sentinel_normalized"] += normalized
    if greedy_disagreement:
        COUNTERS["greedy_disagreement"] += 1
    if data is not None and hasattr(schema, "model_validate"):
        data = schema.model_validate(data)

    choices = None
    if n != 1:
        choices = []
        for ch in response.choices:
            msg = ch.message
            c_text = msg.content or ""
            entry = {"text": c_text, "data": None, "refusal": getattr(msg, "refusal", None), "refusal_detected": None,
                     "value_distributions": None, "greedy_disagreement": False, "error": None}
            if entry["refusal"]:
                entry["error"] = "refusal_field"
            elif schema is not None:
                try:
                    c_data = extract_json(c_text)
                    hit = detect_refusal(c_data, check_schema)
                    if hit:
                        entry["refusal_detected"], entry["error"] = hit, "refusal_detected"
                    else:
                        if want_logprobs:
                            c_data, entry["value_distributions"], entry["greedy_disagreement"] = rerank_enums(
                                c_data, c_text, getattr(ch, "logprobs", None), check_schema)
                        c_data, _n = normalize_sentinels(c_data)
                        entry["data"] = c_data
                except LLMJSONError:
                    entry["error"] = "unparseable"
            choices.append(entry)

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
        "refusal": refusal,
        "schema_mode": response_format is not None,
        "sentinel_normalized": normalized,
        "prompt_hash": p_hash,
        "value_distributions": distributions,  # None = not measured (no strict schema / no enum / no logprobs)
        "greedy_disagreement": greedy_disagreement,
        "choices": choices,  # n > 1 only
    }
