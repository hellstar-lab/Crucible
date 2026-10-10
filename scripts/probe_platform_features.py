"""Phase 4A-plus, P1: test the chat-completions features we never used. Live calls, hard cap.

    python scripts/probe_platform_features.py --dry-run
    python scripts/probe_platform_features.py              # all of P1
    python scripts/probe_platform_features.py --only P1c   # one test

P1a logprobs + top_logprobs=20 at the verdict-value position (strict json_schema; json_object control; neutral prompt)
P1b n=5 in one request vs 5 separate requests (distinctness, cost, latency)
P1c seed (undocumented): same seed twice, different seeds; at temperature 0.8 and at 0
P1d guided_choice via extra_body (undocumented): enforced? coerces like the json_schema enum?
P1e reasoning_effort "none" / "low" with enable_thinking=false
P1f system_fingerprint over 5 streamed runs of one request

Every call is logged to logs/model_calls.jsonl (llm._log_call) and stored verbatim in
docs/platform-features-raw.jsonl. One request per counted call (max_retries=0). No logit_bias anywhere: we measure
the distribution, we do not bend it.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import openai  # noqa: E402

from backend import llm  # noqa: E402
from probe_capabilities import AMBIGUOUS, AMBIGUOUS_NEUTRAL, JSON_OBJECT, _verdict_schema, wrapper  # noqa: E402

CALL_CAP = 80
PLANNED_MAX = 140  # P1 56 + P2-0 6 + P2-2..P2-4 (approved batch)
TIERS = ["nano", "super", "ultra"]
RAW = ROOT / "docs" / "platform-features-raw.jsonl"
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
ENUM_A = ["supported", "unsupported", "unclear"]
SCHEMA_A = wrapper("verdict", _verdict_schema(ENUM_A))
SHORT_CREATIVE = "Write one sentence describing a caching layer for a payments API."


class Budget(Exception):
    pass


class Prober:
    def __init__(self, already: int):
        self.calls = already
        self.records: list[dict] = []
        self.clients = {t: llm.get_client(t).with_options(max_retries=0) for t in TIERS}

    def call(self, test: str, tier: str, prompt: str, *, stream: bool = False, **kw) -> dict:
        if self.calls >= CALL_CAP:
            raise Budget(f"call cap {CALL_CAP} reached before {test}/{tier}")
        self.calls += 1
        model = llm.MODELS[tier]["id"]
        extra = dict(NO_THINK)
        extra.update(kw.pop("extra_body", {}))
        base = {"test": test, "tier": tier, "prompt": prompt, "params": {k: v for k, v in kw.items()}, "extra_body": extra, "stream": stream}
        t0 = time.perf_counter()
        try:
            if stream:
                chunks, fingerprints, content, usage = [], [], [], None
                s = self.clients[tier].chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], stream=True,
                                                                stream_options={"include_usage": True}, extra_body=extra, **kw)
                for ch in s:
                    fingerprints.append(getattr(ch, "system_fingerprint", None))
                    if ch.choices and ch.choices[0].delta and ch.choices[0].delta.content:
                        content.append(ch.choices[0].delta.content)
                    if getattr(ch, "usage", None):
                        usage = ch.usage
                    chunks.append(1)
                lat = time.perf_counter() - t0
                tin, tout = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
                rec = {**base, "ok": True, "latency_s": round(lat, 4), "contents": ["".join(content)],
                       "system_fingerprints": sorted({f for f in fingerprints if f is not None}),
                       "fingerprint_none_chunks": sum(1 for f in fingerprints if f is None), "chunks": len(chunks),
                       "input_tokens": tin, "output_tokens": tout}
            else:
                resp = self.clients[tier].chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                                                   extra_body=extra, **kw)
                lat = time.perf_counter() - t0
                d = resp.model_dump()
                u = d.get("usage") or {}
                rec = {**base, "ok": True, "latency_s": round(lat, 4), "system_fingerprint": d.get("system_fingerprint"),
                       "contents": [c["message"].get("content") for c in d["choices"]],
                       "reasoning": [c["message"].get("reasoning_content") or c["message"].get("reasoning") for c in d["choices"]],
                       "finish_reasons": [c.get("finish_reason") for c in d["choices"]],
                       "logprobs": [c.get("logprobs") for c in d["choices"]],
                       "input_tokens": u.get("prompt_tokens") or 0, "output_tokens": u.get("completion_tokens") or 0,
                       "completion_tokens_details": u.get("completion_tokens_details"), "usage_raw": u,
                       "top_level_keys": sorted(k for k, v in d.items() if v is not None)}
            rec["cost_usd"] = llm.estimate_cost(tier, rec["input_tokens"], rec["output_tokens"])
        except openai.APIStatusError as exc:
            rec = {**base, "ok": False, "status": exc.status_code, "error": str(exc.message)[:500], "latency_s": round(time.perf_counter() - t0, 4),
                   "cost_usd": None, "input_tokens": 0, "output_tokens": 0}
        except openai.APIError as exc:
            rec = {**base, "ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"[:500], "latency_s": round(time.perf_counter() - t0, 4),
                   "cost_usd": None, "input_tokens": 0, "output_tokens": 0}
        llm._log_call({"task": "probe_platform", "tier": tier, "model": model, "run_tag": f"4A+:{test}", "ok": rec["ok"],
                       "latency_s": rec["latency_s"], "input_tokens": rec["input_tokens"], "output_tokens": rec["output_tokens"],
                       "cost_usd": rec["cost_usd"], "prompt_hash": llm.prompt_hash(prompt), "temperature": kw.get("temperature")})
        self.records.append(rec)
        return rec


# ---- verdict-position analysis -------------------------------------------------------------------------------

def verdict_positions(logprobs: dict | None, n_positions: int = 3) -> list[dict] | None:
    """The token positions that spell the verdict value: [{index, token, logprob, prob, top: [(token, logprob, prob)]}]."""
    if not logprobs or not logprobs.get("content"):
        return None
    toks = logprobs["content"]
    text, starts = "", []
    for t in toks:
        starts.append(len(text))
        text += t["token"]
    m = re.search(r'"verdict"\s*:\s*"', text)
    if not m:
        return None
    vstart = m.end()
    i = max(j for j, s in enumerate(starts) if s <= vstart)  # the token covering the value's first character
    if starts[i] + len(toks[i]["token"]) <= vstart:
        i += 1
    out = []
    for j in range(i, min(i + n_positions, len(toks))):
        t = toks[j]
        if j > i and '"' in t["token"]:
            break
        out.append({"index": j, "token": t["token"], "logprob": t["logprob"], "prob": math.exp(t["logprob"]),
                    "top": [(x["token"], x["logprob"], math.exp(x["logprob"])) for x in (t.get("top_logprobs") or [])]})
    return out


def enum_probabilities(positions: list[dict] | None, enum: list[str]) -> dict:
    """Per enum value: the probability along the generated path where available, else the first-token mass of tokens
    that are a prefix of the value (an upper bound, since later tokens were not observed)."""
    if not positions:
        return {}
    first = positions[0]
    res = {}
    chosen_path = "".join(p["token"] for p in positions).lstrip('"')
    for value in enum:
        pref_mass = sum(p for tok, _lp, p in first["top"] if tok.lstrip('"') and value.startswith(tok.lstrip('"')))
        if chosen_path.startswith(value) or value.startswith(chosen_path[:len(value)]) and chosen_path[:len(value)] == value:
            prob, path = 1.0, ""
            for p in positions:
                path += p["token"].lstrip('"') if not path else p["token"]
                prob *= p["prob"]
                if path.startswith(value):
                    break
            res[value] = {"probability": prob, "how": "along the generated path"}
        else:
            # the value diverges from the generated path at some position k: P(prefix up to k) * P(divergent token at k)
            path, prob, found = "", 1.0, None
            for p in positions:
                for tok, _lp, pr in p["top"]:
                    cand = (path + (tok.lstrip('"') if not path else tok))
                    if cand and value.startswith(cand) and not chosen_path.startswith(cand):
                        found = (prob * pr, len(cand))
                        break
                if found:
                    break
                path += p["token"].lstrip('"') if not path else p["token"]
                prob *= p["prob"]
            res[value] = ({"probability": found[0], "how": f"upper bound: prefix of {found[1]} chars observed, rest not"} if found
                          else {"probability": None, "how": "not in the top-20 at any observed position"})
        res[value]["first_token_prefix_mass"] = round(pref_mass, 6)
    return res


# ---- tests ---------------------------------------------------------------------------------------------------

def p1a(pr: Prober):
    for tier in TIERS:
        pr.call("P1a_schema_T3prompt", tier, AMBIGUOUS, response_format=SCHEMA_A, temperature=0, max_tokens=300, logprobs=True, top_logprobs=20)
        pr.call("P1a_jsonobject_T3prompt", tier, AMBIGUOUS, response_format=JSON_OBJECT, temperature=0, max_tokens=300, logprobs=True, top_logprobs=20)
        pr.call("P1a_schema_neutral", tier, AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0, max_tokens=300, logprobs=True, top_logprobs=20)


def p1b(pr: Prober):
    for tier in TIERS:
        pr.call("P1b_n5_single", tier, AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0, max_tokens=300, n=5)
        for i in range(5):
            pr.call("P1b_n1_separate", tier, AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0, max_tokens=300)


def p1c(pr: Prober):
    for seed in (12345, 12345, 99999, 99999):
        pr.call(f"P1c_seed{seed}_t0.8", "nano", SHORT_CREATIVE, temperature=0.8, max_tokens=80, seed=seed)
    for _ in range(2):
        pr.call("P1c_seed12345_t0", "nano", AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0, max_tokens=300, seed=12345)


def p1d(pr: Prober):
    for tier in TIERS:
        pr.call("P1d_guided_choice_3", tier, AMBIGUOUS_NEUTRAL, temperature=0, max_tokens=20, logprobs=True, top_logprobs=20,
                extra_body={"guided_choice": ENUM_A})
        pr.call("P1d_guided_choice_2", tier, AMBIGUOUS_NEUTRAL, temperature=0, max_tokens=20, logprobs=True, top_logprobs=20,
                extra_body={"guided_choice": ["supported", "unsupported"]})


def p1e(pr: Prober):
    for tier in TIERS:
        for effort in ("none", "low"):
            pr.call(f"P1e_reasoning_{effort}", tier, AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0, max_tokens=300,
                    reasoning_effort=effort)


def p1f(pr: Prober):
    for _ in range(5):
        pr.call("P1f_stream_fingerprint", "nano", AMBIGUOUS_NEUTRAL, stream=True, response_format=SCHEMA_A, temperature=0, max_tokens=300)


def p1b2(pr: Prober):
    """Follow-up after P1b: nano caps n at 3 for non-dedicated models; super/ultra reject n>1 under greedy sampling."""
    for tier in TIERS:
        pr.call("P1b_n3_t0.7", tier, AMBIGUOUS_NEUTRAL, response_format=SCHEMA_A, temperature=0.7, max_tokens=300, n=3)


def p1d2(pr: Prober):
    """Follow-up after P1d: guided_choice was silently ignored; newer vLLM moved it to structured_outputs.choice."""
    for tier in TIERS:
        pr.call("P1d_structured_outputs_choice_2", tier, AMBIGUOUS_NEUTRAL, temperature=0, max_tokens=20, logprobs=True, top_logprobs=20,
                extra_body={"structured_outputs": {"choice": ["supported", "unsupported"]}})


ENUM_G1 = ["supported", "unsupported", "insufficient_evidence"]


def p20(pr: Prober):
    """P2-0: G1's enum, re-measured with logprobs (G1 concluded from generated values only)."""
    for tier in TIERS:
        for name, prompt in (("neutral", AMBIGUOUS_NEUTRAL), ("T3prompt", AMBIGUOUS)):
            pr.call(f"P2-0_G1enum_{name}", tier, prompt, response_format=wrapper("verdict", _verdict_schema(ENUM_G1)), temperature=0,
                    max_tokens=300, logprobs=True, top_logprobs=20)


ENUM_P22 = ["supported", "contradicted", "no_evidence", "inconclusive"]


def p22(pr: Prober):
    """P2-2: do the models reach for "inconclusive"? Neutral prompt, logprobs, 3 tiers."""
    for tier in TIERS:
        pr.call("P2-2_new_enum_neutral", tier, AMBIGUOUS_NEUTRAL, response_format=wrapper("verdict", _verdict_schema(ENUM_P22)),
                temperature=0, max_tokens=300, logprobs=True, top_logprobs=20)


VERDICT_RUBRIC = ("Labels (exactly one applies): supported: the evidence establishes the claim. contradicted: the evidence "
                  "establishes the opposite of the claim. no_evidence: the evidence says nothing about the claim. inconclusive: the "
                  "evidence bears on the claim but does not let you determine whether it is true.")


def p22r(pr: Prober):
    """P2-2 with the label rubric (every judgement prompt defines all labels; no hint about the instance)."""
    for tier in TIERS:
        pr.call("P2-2_new_enum_neutral_rubric", tier, AMBIGUOUS_NEUTRAL + " " + VERDICT_RUBRIC,
                response_format=wrapper("verdict", _verdict_schema(ENUM_P22)), temperature=0, max_tokens=300, logprobs=True, top_logprobs=20)


TESTS = {"P2-2r": (p22r, 3), "P2-2": (p22, 3), "P2-0": (p20, 6), "P1d2": (p1d2, 3), "P1b2": (p1b2, 3), "P1a": (p1a, 9), "P1b": (p1b, 18), "P1c": (p1c, 6), "P1d": (p1d, 6), "P1e": (p1e, 6), "P1f": (p1f, 5)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="comma-separated tests, e.g. P1a,P1c")
    a = ap.parse_args()
    chosen = a.only.split(",") if a.only else list(TESTS)
    prior = [json.loads(line) for line in RAW.read_text().splitlines()] if RAW.exists() else []
    planned = sum(TESTS[t][1] for t in chosen)
    print(f"prior calls: {len(prior)}; planned now: {planned}; budget {PLANNED_MAX}, cap {CALL_CAP}")
    if len(prior) + planned > PLANNED_MAX:
        print("over the planned budget; refusing")
        return 2
    if a.dry_run:
        return 0
    pr = Prober(len(prior))
    try:
        for t in chosen:
            TESTS[t][0](pr)
    except Budget as exc:
        print(f"ABORT: {exc}")
    with RAW.open("a", encoding="utf-8") as fh:
        for r in pr.records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"calls made now: {len(pr.records)}; total: {pr.calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
