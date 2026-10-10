"""Phase 4.0: measure what structured output actually does on each Nebius tier.

    python scripts/probe_capabilities.py            # runs the probe (live calls, hard cap below)
    python scripts/probe_capabilities.py --dry-run  # prints the call plan and the count, no calls

The question is not "does the call error" but "is the schema ENFORCED". So the enforcement tests send
the schema ONLY in ``response_format`` (a prompt that also states the schema would let the model obey by
instruction), tempt the model to break it, and compare against a json_object control that has no schema.

Outputs (committed, read by backend/llm.py and by humans):
    backend/model_capabilities.json        machine-readable matrix
    docs/nebius-capabilities-raw.jsonl     every request/reply, verbatim (prompts are public test strings)
    docs/nebius-capabilities.md            written by hand from these results

Every call is also logged to logs/model_calls.jsonl by the same writer llm.py uses. No credential is
printed or stored. Each probe client has max_retries=0 so one counted call is exactly one request.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openai  # noqa: E402

from backend import llm  # noqa: E402

CALL_CAP = 60  # hard cap for ALL of Part 1
STOP_AT = 56  # refuse to start a call that could breach the cap with retries of the plan still pending
TIERS = ["nano", "super", "ultra"]
RAW = ROOT / "docs" / "nebius-capabilities-raw.jsonl"
OUT = ROOT / "backend" / "model_capabilities.json"
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def wrapper(name: str, schema: dict) -> dict:
    """response_format for json_schema, written from the spec (not copied from the docs)."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


JSON_OBJECT = {"type": "json_object"}

# ---- schemas -------------------------------------------------------------------------------------
S_FLAT = {"type": "object", "properties": {"city": {"type": "string"}, "population_millions": {"type": "number"}, "is_capital": {"type": "boolean"}},
          "required": ["city", "population_millions", "is_capital"], "additionalProperties": False}
S_ENUM = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["supported", "unsupported"]}, "reason": {"type": "string"}},
          "required": ["verdict", "reason"], "additionalProperties": False}
S_NOEXTRA = {"type": "object", "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
             "required": ["name", "count"], "additionalProperties": False}
S_REQ = {"type": "object", "properties": {"name": {"type": "string"}, "count": {"type": "integer"}, "note": {"type": "string"}},
         "required": ["name", "count", "note"], "additionalProperties": False}
S_NESTED = {"type": "object", "properties": {"claim": {"type": "object", "properties": {"subject": {"type": "string"}, "qualifier": {"type": "string"}},
                                                      "required": ["subject", "qualifier"], "additionalProperties": False}, "ok": {"type": "boolean"}},
            "required": ["claim", "ok"], "additionalProperties": False}
KINDS = ["technology_use", "metric", "ownership", "scale"]
S_ARRAY = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "object", "properties": {
    "text": {"type": "string"}, "kind": {"type": "string", "enum": KINDS}}, "required": ["text", "kind"], "additionalProperties": False}}},
    "required": ["claims"], "additionalProperties": False}
S_PAGE = {"type": "object", "properties": {"title": {"type": "string"}, "fetched_at": {"type": "string"}},
          "required": ["title", "fetched_at"], "additionalProperties": False}
INABILITY_PROMPT = "Open https://example.com in a web browser right now and return the exact page title and the time you fetched it, as JSON."

AMBIGUOUS = ("Claim: 'The service handles 12k requests per second.' Evidence: 'The README says it is fast. There is no benchmark "
             "file and no load test.' Reply as JSON with fields verdict and reason. The honest verdict here is 'unclear', "
             "so use that word if the evidence does not decide it.")
AMBIGUOUS_WITH_SCHEMA = AMBIGUOUS + (" Schema: " + json.dumps(S_ENUM) + " verdict must be exactly one of the enum values.")


# ---- checks: each returns (matches_schema: bool, detail: str) --------------------------------------
def parse(content):
    if content is None:
        return None, "content is None"
    try:
        return json.loads(content), "parsed directly"
    except json.JSONDecodeError:
        try:
            return llm.extract_json(content), "parsed only after fence/prose stripping"
        except llm.LLMJSONError:
            return None, "unparseable"


def check_flat(d):
    ok = isinstance(d, dict) and set(d) == {"city", "population_millions", "is_capital"} and isinstance(d["city"], str) \
        and isinstance(d["population_millions"], (int, float)) and isinstance(d["is_capital"], bool)
    return ok, "flat object with 3 typed fields" if ok else f"bad shape: {d!r}"


def check_enum(d):
    ok = isinstance(d, dict) and set(d) == {"verdict", "reason"} and d["verdict"] in ("supported", "unsupported") and isinstance(d["reason"], str)
    return ok, f"verdict={d.get('verdict')!r}" if isinstance(d, dict) else f"not an object: {d!r}"


def check_noextra(d):
    extra = sorted(set(d) - {"name", "count"}) if isinstance(d, dict) else None
    return (isinstance(d, dict) and not extra and isinstance(d.get("name"), str) and isinstance(d.get("count"), int)), f"extra keys: {extra}"


def check_req(d):
    missing = sorted({"name", "count", "note"} - set(d)) if isinstance(d, dict) else None
    return (isinstance(d, dict) and not missing), f"missing keys: {missing}"


def check_nested(d):
    c = d.get("claim") if isinstance(d, dict) else None
    ok = isinstance(c, dict) and set(c) == {"subject", "qualifier"} and isinstance(d.get("ok"), bool)
    return ok, "nested object present with both keys" if ok else f"bad shape: {d!r}"


def check_array(d):
    cl = d.get("claims") if isinstance(d, dict) else None
    ok = isinstance(cl, list) and len(cl) >= 1 and all(isinstance(x, dict) and set(x) == {"text", "kind"} and x["kind"] in KINDS for x in cl)
    return ok, f"{len(cl) if isinstance(cl, list) else 'no'} claim objects" if ok else f"bad shape: {d!r}"


# ---- call plan -----------------------------------------------------------------------------------
# (test_id, repeats, prompt, response_format, checker). Order: cheap and informative first.
PLAN = [
    ("T1_json_object", 1, "Reply with a JSON object with the keys color and hex for the colour red.", JSON_OBJECT, None),
    ("T2_json_schema_flat", 1, "Give the city Tokyo with its population in millions and whether it is a capital, as JSON.",
     wrapper("city_facts", S_FLAT), check_flat),
    ("T3a_enum_param_only", 3, AMBIGUOUS, wrapper("verdict", S_ENUM), check_enum),
    ("T3b_enum_param_and_prompt", 3, AMBIGUOUS_WITH_SCHEMA, wrapper("verdict", S_ENUM), check_enum),
    ("T3c_control_json_object", 1, AMBIGUOUS, JSON_OBJECT, check_enum),
    ("T4_additional_props", 1, "Return JSON with name 'cache' and count 3. Also add an extra field called confidence with the number 0.9.",
     wrapper("item", S_NOEXTRA), check_noextra),
    ("T5_required_fields", 1, "Return JSON with name 'cache' and count 3. Leave out the field note entirely; do not include it.",
     wrapper("item_note", S_REQ), check_req),
    ("T6_nested", 1, "Subject: Redis cache. Qualifier: on the read path. Return JSON with claim (subject, qualifier) and ok true.",
     wrapper("nested", S_NESTED), check_nested),
    ("T7_array_of_objects", 1, "Extract claims from: 'Built a Redis cache serving 12k req/s and cut p99 latency by 40%.' "
     "Each claim has text and a kind.", wrapper("claims", S_ARRAY), check_array),
    # The first run used a harmful-content prompt (nano and super complied; ultra refused inside the schema). Its output was
    # deleted and this harmless inability probe replaces it: complying would only mean a hallucinated page title.
    ("T8_refusal", 1, INABILITY_PROMPT, wrapper("page", S_PAGE), None),
]


# Controls added AFTER the first run showed T4/T5 had none: the same prompts with json_object and no schema.
# They are expected to VIOLATE the schema (add the extra key / omit note); that is what proves T4/T5 were enforcement.
CONTROLS = [
    ("T4c_control_json_object", 1, PLAN[5][2], JSON_OBJECT, check_noextra),
    ("T5c_control_json_object", 1, PLAN[6][2], JSON_OBJECT, check_req),
]


# ---- Phase 4.0b gaps -----------------------------------------------------------------------------
GAP_CAP = 28  # 30 for Part 1b, minus the 2 metadata calls made by --prices
S_ABSTAIN = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["supported", "unsupported", "insufficient_evidence"]},
                                              "reason": {"type": "string"}}, "required": ["verdict", "reason"], "additionalProperties": False}


def _pydantic_shapes():
    from typing import Literal

    from pydantic import BaseModel

    class Sub(BaseModel):
        text: str
        kind: Literal["technology_use", "metric"]

    class ShapeA(BaseModel):
        claim: Sub
        ok: bool

    class ShapeC(BaseModel):
        claims: list[Sub]

    a = ShapeA.model_json_schema()  # exactly as pydantic generates it: $defs + $ref, no additionalProperties
    b = json.loads(json.dumps(a))
    b["properties"]["claim"] = b.pop("$defs")["Sub"]  # (b) the same schema with the $ref inlined by hand
    return a, b, ShapeC.model_json_schema()


def check_kind(d, many=False):
    items = (d.get("claims") if isinstance(d, dict) else None) if many else [d.get("claim") if isinstance(d, dict) else None]
    if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
        return False, f"bad shape: {d!r}"
    kinds = [x.get("kind") for x in items]
    return all(k in ("technology_use", "metric") for k in kinds), f"kinds={kinds}"


def gap_plan():
    a, b, c = _pydantic_shapes()
    tempt_one = "Claim text: 'Passionate about clean code'. Return JSON with claim (text, kind) and ok. The kind is 'opinion'."
    tempt_many = ("Extract claims from: 'Built a Redis cache. I love clean code.' Return JSON with claims, each with text and kind. "
                  "Use kind 'opinion' for the second claim.")
    return [
        ("G1_abstain_enum", 3, AMBIGUOUS, wrapper("verdict", S_ABSTAIN), check_enum_abstain),
        ("G2a_pydantic_ref", 1, tempt_one, wrapper("shape_a", a), lambda d: check_kind(d)),
        ("G2b_inlined", 1, tempt_one, wrapper("shape_b", b), lambda d: check_kind(d)),
        ("G2c_list_of_submodels", 1, tempt_many, wrapper("shape_c", c), lambda d: check_kind(d, many=True)),
        ("T8b_inability", 1, INABILITY_PROMPT, wrapper("page", S_PAGE), None),
    ]


def check_enum_abstain(d):
    ok = isinstance(d, dict) and d.get("verdict") in ("supported", "unsupported", "insufficient_evidence")
    return ok, f"verdict={d.get('verdict')!r}" if isinstance(d, dict) else f"not an object: {d!r}"


_REFUSAL_HEAD = re.compile(r"\b(I|I'm|I am)\b.{0,20}\b(cannot|can't|unable|not able|won't)\b", re.I)


# ---- Phase 4.0c: G5 abstain spelling ------------------------------------------------------------------
G5_CAP = 20  # all of Phase 4.0c


def _verdict_schema(enum):
    return {"type": "object", "properties": {"verdict": {"type": "string", "enum": enum}, "reason": {"type": "string"}},
            "required": ["verdict", "reason"], "additionalProperties": False}


G5_ARMS = {
    "A": ["supported", "unsupported", "unclear"],        # the word the models produce unconstrained
    "B": ["supported", "unsupported", "indeterminate"],  # distinct leading token (prefix lock-in diagnostic)
    "C": ["indeterminate", "supported", "unsupported"],  # abstain value first (ordering control)
}


# The T3/G1 prompt names 'unclear' ("use that word"), so arm A on it measures instruction-following as well.
# The neutral prompt removes that sentence: no abstain word appears anywhere in it.
AMBIGUOUS_NEUTRAL = AMBIGUOUS.split(" The honest verdict")[0]
assert "unclear" not in AMBIGUOUS_NEUTRAL


def check_verdict_in(enum):
    def check(d):
        ok = isinstance(d, dict) and d.get("verdict") in enum
        return ok, f"verdict={d.get('verdict')!r}" if isinstance(d, dict) else f"not an object: {d!r}"
    return check


def scrub_harmful(records):
    """Delete the phishing-probe output (content and parsed); keep only the boolean findings."""
    for r in records:
        if r["test"] == "T8_refusal" and r.get("ok") and "phishing" in (r.get("prompt") or "").lower():
            head = r.get("content") or ""
            if "refused_in_content" not in r:
                r["refused_in_content"] = bool(_REFUSAL_HEAD.search(head[:200]))
                r["complied"] = not r["refused_in_content"]
            for k in ("content", "parsed", "detail", "parse_how", "content_chars", "content_truncated"):
                r.pop(k, None)
            r["content_deleted"] = "harmful-content probe: output deleted; only refused_in_content / complied kept"
    return records


def plan_size() -> int:
    return sum(r for _t, r, *_ in PLAN) * len(TIERS)


class Probe:
    def __init__(self, already_made=0):
        self.calls = already_made
        self.records: list[dict] = []
        self.clients = {t: llm.get_client(t).with_options(max_retries=0) for t in TIERS}

    def call(self, tier, test_id, prompt, response_format, run):
        if self.calls >= STOP_AT:
            raise SystemExit(f"ABORT: {self.calls} calls made, approaching the cap of {CALL_CAP}; stopping before {test_id}")
        self.calls += 1
        model = llm.MODELS[tier]["id"]
        base = {"test": test_id, "tier": tier, "run": run, "prompt": prompt, "response_format": response_format}
        start = time.perf_counter()
        try:
            resp = self.clients[tier].chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], max_tokens=400, temperature=0,
                response_format=response_format, extra_body=NO_THINK)
        except openai.APIStatusError as exc:
            rec = {**base, "ok": False, "status": exc.status_code, "error": str(exc.message)[:400], "latency_s": round(time.perf_counter() - start, 3)}
            llm._log_call({"task": "probe_capabilities", "tier": tier, "model": model, "run_tag": f"4.0:{test_id}:{run}", "ok": False,
                           "error": f"HTTP {exc.status_code}", "latency_s": rec["latency_s"]})
        except openai.APIError as exc:
            rec = {**base, "ok": False, "status": None, "error": type(exc).__name__, "latency_s": round(time.perf_counter() - start, 3)}
            llm._log_call({"task": "probe_capabilities", "tier": tier, "model": model, "run_tag": f"4.0:{test_id}:{run}", "ok": False,
                           "error": type(exc).__name__, "latency_s": rec["latency_s"]})
        else:
            latency = time.perf_counter() - start
            msg = resp.choices[0].message
            dumped = msg.model_dump()
            content = msg.content
            reasoning = dumped.get("reasoning_content") or dumped.get("reasoning")
            u = resp.usage
            tin, tout = (u.prompt_tokens or 0), (u.completion_tokens or 0)
            cost = llm.estimate_cost(tier, tin, tout)
            rec = {**base, "ok": True, "content": content, "refusal": dumped.get("refusal"), "finish_reason": resp.choices[0].finish_reason,
                   "has_think_tag": bool(content and "<think" in content.lower()), "has_reasoning_content": bool(reasoning),
                   "fenced": bool(content and content.lstrip().startswith("```")), "input_tokens": tin, "output_tokens": tout,
                   "latency_s": round(latency, 4), "cost_usd": cost, "extra_message_fields": sorted(k for k, v in dumped.items() if v not in (None, [], "") and k not in ("role", "content"))}
            llm._log_call({"task": "probe_capabilities", "tier": tier, "model": model, "run_tag": f"4.0:{test_id}:{run}", "ok": True,
                           "latency_s": round(latency, 4), "input_tokens": tin, "output_tokens": tout, "cost_usd": cost,
                           "finish_reason": resp.choices[0].finish_reason})
        self.records.append(rec)
        return rec


def evaluate(rec, checker):
    """Add parse/schema outcome to a record in place."""
    if not rec["ok"]:
        rec.update(parsed=None, schema_ok=False, detail=f"HTTP {rec.get('status')}: {rec.get('error')}")
        return
    data, how = parse(rec["content"])
    rec["parsed"], rec["parse_how"] = data, how
    if checker is None:
        rec["schema_ok"], rec["detail"] = data is not None, how
    elif data is None:
        rec["schema_ok"], rec["detail"] = False, how
    else:
        rec["schema_ok"], rec["detail"] = checker(data)


def build_capabilities(records) -> dict:
    caps = {}
    for tier in TIERS:
        r = [x for x in records if x["tier"] == tier]
        by = lambda t: [x for x in r if x["test"] == t]  # noqa: E731
        t1, t2 = by("T1_json_object")[0], by("T2_json_schema_flat")[0]
        t3a, t4, t5, t6, t7 = by("T3a_enum_param_only"), by("T4_additional_props")[0], by("T5_required_fields")[0], by("T6_nested")[0], by("T7_array_of_objects")[0]
        refused = [x for x in by("T8_refusal") if x["ok"] and x.get("refusal")]
        lat = [x["latency_s"] for x in t3a if x["ok"]]
        cin = llm.PRICING.get(tier)
        caps[tier] = {
            "json_object": bool(t1["ok"] and t1.get("schema_ok")),
            "json_object_needs_fence_stripping": bool(t1["ok"] and (t1["fenced"] or t1.get("parse_how") != "parsed directly")),
            "json_schema": bool(t2["ok"] and t2.get("schema_ok")),
            "strict_enforced": bool(all(x["ok"] and x["schema_ok"] for x in t3a)) and len(t3a) == 3,
            "additional_props_enforced": bool(t4["ok"] and t4["schema_ok"]),
            "required_enforced": bool(t5["ok"] and t5["schema_ok"]),
            "additional_props_control_violates": next((not x["schema_ok"] for x in by("T4c_control_json_object") if x["ok"]), None),
            "required_control_violates": next((not x["schema_ok"] for x in by("T5c_control_json_object") if x["ok"]), None),
            "strict_control_violates": next((not x["schema_ok"] for x in by("T3c_control_json_object") if x["ok"]), None),
            "nested_ok": bool(t6["ok"] and t6["schema_ok"]),
            "array_of_objects_ok": bool(t7["ok"] and t7["schema_ok"]),
            "refusal_field_seen": bool(refused),
            "refuses_inside_schema_content": bool(any(x["ok"] and (x.get("refused_in_content") if "refused_in_content" in x
                                                                    else _REFUSAL_HEAD.search((x.get("content") or "")[:200])) for x in by("T8_refusal"))),
            "abstain_reached": ([x["parsed"].get("verdict") == "insufficient_evidence" for x in by("G1_abstain_enum") if x["ok"] and isinstance(x.get("parsed"), dict)]
                                or None),
            "ref_accepted": next((x["ok"] for x in by("G2a_pydantic_ref")), None),
            "ref_enforced": next((bool(x["ok"] and x["schema_ok"]) for x in by("G2a_pydantic_ref")), None),
            "inlined_enforced": next((bool(x["ok"] and x["schema_ok"]) for x in by("G2b_inlined")), None),
            "list_of_submodels_enforced": next((bool(x["ok"] and x["schema_ok"]) for x in by("G2c_list_of_submodels")), None),
            "thinking_leaks_with_schema": bool(any(x["ok"] and (x["has_think_tag"] or x["has_reasoning_content"]) for x in r)),
            "median_latency_ms": round(statistics.median(lat) * 1000) if lat else None,
            "cost_per_1k_in": round(cin[0] / 1000, 8) if cin else None,
            "cost_per_1k_out": round(cin[1] / 1000, 8) if cin else None,
            # from stored token counts and the CURRENT llm.PRICING, so a price found later applies to past calls
            "median_cost_per_call_usd": (round(statistics.median([llm.estimate_cost(tier, x["input_tokens"], x["output_tokens"]) for x in t3a if x["ok"]]), 8)
                                         if cin and any(x["ok"] for x in t3a) else None),
        }
    return caps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prices", action="store_true", help="read per-model pricing from GET /models?verbose=true (2 metadata calls)")
    ap.add_argument("--g5", help="Phase 4.0c abstain-spelling arms, e.g. A,B,C")
    ap.add_argument("--tiers", help="comma-separated subset of tiers for --g5")
    ap.add_argument("--runs", type=int, default=1, help="runs per tier per arm for --g5")
    ap.add_argument("--gaps", action="store_true", help="Phase 4.0b: G1 abstain enum, G2 $defs/$ref, harmless refusal probe")
    ap.add_argument("--rebuild", action="store_true", help="re-derive backend/model_capabilities.json from the stored raw file (no calls)")
    ap.add_argument("--extra-controls", action="store_true", help="append the T4/T5 controls to an existing run (counts the earlier calls)")
    args = ap.parse_args()
    if args.prices:
        import httpx

        key = llm.get_client("nano").api_key  # same key handling as llm.py; never printed
        wanted = {m["id"]: t for t, m in llm.MODELS.items()}
        for base in sorted({m["base_url"] for m in llm.MODELS.values()}):
            r = httpx.get(base.rstrip("/") + "/models", params={"verbose": "true"}, headers={"Authorization": f"Bearer {key}"}, timeout=30)
            print(f"GET {base}models?verbose=true -> HTTP {r.status_code}")
            if r.status_code != 200:
                continue
            for m in r.json().get("data", []):
                if m.get("id") in wanted:
                    print(f"  {wanted[m['id']]:<6} {m['id']}: pricing={m.get('pricing')}")
        return 0
    if args.g5:
        records = [json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines()]
        used = sum(1 for r in records if r["test"].startswith("G5"))
        arms = args.g5.split(",")
        runs = args.runs
        tiers = args.tiers.split(",") if args.tiers else TIERS
        n = len(arms) * len(tiers) * runs
        print(f"G5 calls so far: {used}; planned now: {n}; cap {G5_CAP}")
        if used + n > G5_CAP:
            print("would exceed the cap; refusing")
            return 2
        probe = Probe()
        for arm in arms:
            neutral = arm.endswith("n")  # "An" = arm A on the neutral prompt; "N0" = neutral prompt, json_object, no schema
            base_arm = arm.rstrip("n")
            prompt = AMBIGUOUS_NEUTRAL if (neutral or arm == "N0") else AMBIGUOUS
            rf = JSON_OBJECT if arm == "N0" else wrapper("verdict", _verdict_schema(G5_ARMS[base_arm]))
            checker = check_verdict_in(["supported", "unsupported"]) if arm == "N0" else check_verdict_in(G5_ARMS[base_arm])
            for tier in tiers:
                for i in range(runs):
                    run = sum(1 for r in records + probe.records if r["test"] == f"G5{arm}" and r["tier"] == tier) + 1
                    rec = probe.call(tier, f"G5{arm}", prompt, rf, run)
                    evaluate(rec, checker)
                    print(f"G5{arm} {tier:<6} run{run} {rec.get('content')!r}"[:600])
        records += probe.records
        RAW.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        print(f"G5 calls total: {used + probe.calls} / {G5_CAP}")
        return 0
    if args.gaps:
        records = scrub_harmful([json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines()])
        plan = gap_plan()
        n = sum(r for _t, r, *_ in plan) * len(TIERS)
        print(f"gap calls planned: {n} (cap {GAP_CAP})")
        if n > GAP_CAP:
            return 2
        probe = Probe()
        for test_id, repeats, prompt, rf, checker in plan:
            for tier in TIERS:
                for run in range(1, repeats + 1):
                    if probe.calls >= GAP_CAP:
                        raise SystemExit(f"ABORT at {probe.calls} gap calls")
                    rec = probe.call(tier, test_id, prompt, rf, run)
                    evaluate(rec, checker)
                    print(f"{test_id:<24}{tier:<7}run{run} ok={rec['ok']} schema_ok={rec.get('schema_ok')} {str(rec.get('detail'))[:60]} :: {rec.get('content')!r}"[:400])
        records += probe.records
        RAW.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        OUT.write_text(json.dumps(build_capabilities(records), indent=2) + "\n")
        print(f"gap calls made: {probe.calls}")
        return 0
    if args.rebuild:
        records = [json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines()]
        OUT.write_text(json.dumps(build_capabilities(records), indent=2) + "\n")
        print(f"rebuilt from {len(records)} stored calls; no API calls made")
        return 0
    if args.extra_controls:
        prior = [json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines()]
        n = len(CONTROLS) * len(TIERS)
        print(f"prior calls: {len(prior)}; adding {n}; total {len(prior) + n} (cap {CALL_CAP})")
        if len(prior) + n > CALL_CAP:
            return 2
        probe = Probe(already_made=len(prior))
        for test_id, repeats, prompt, rf, checker in CONTROLS:
            for tier in TIERS:
                rec = probe.call(tier, test_id, prompt, rf, 1)
                evaluate(rec, checker)
                print(f"{test_id:<28}{tier:<7} ok={rec['ok']} schema_ok={rec.get('schema_ok')} {str(rec.get('detail'))[:90]} :: {rec.get('content')!r}"[:230])
        records = scrub_harmful(prior + probe.records)
        for r in records:  # re-derive schema_ok for prior rows is unnecessary: they were evaluated before being written
            pass
        RAW.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        caps = build_capabilities(records)
        OUT.write_text(json.dumps(caps, indent=2) + "\n")
        print(f"\ncalls made in total: {len(records)} / {CALL_CAP}")
        print(json.dumps({t: {k: v for k, v in c.items() if "control" in k} for t, c in caps.items()}, indent=1))
        return 0
    n = plan_size()
    print(f"planned calls: {n} (cap {CALL_CAP})")
    if n > CALL_CAP:
        print("plan exceeds the cap; refusing")
        return 2
    if args.dry_run:
        for t, rep, prompt, *_ in PLAN:
            print(f"  {t} x{rep} per tier  prompt={len(prompt)} chars")
        return 0
    probe = Probe()
    for test_id, repeats, prompt, rf, checker in PLAN:
        for tier in TIERS:
            for run in range(1, repeats + 1):
                rec = probe.call(tier, test_id, prompt, rf, run)
                evaluate(rec, checker)
                print(f"{test_id:<28}{tier:<7}run{run} ok={rec['ok']} schema_ok={rec.get('schema_ok')} {str(rec.get('detail'))[:70]}")
    # T8 follow-up: if no tier showed a refusal field on the first prompt, report that; do not escalate silently.
    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", encoding="utf-8") as fh:
        for rec in probe.records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    caps = build_capabilities(probe.records)
    OUT.write_text(json.dumps(caps, indent=2) + "\n")
    print(f"\ncalls made: {probe.calls} / {CALL_CAP}")
    total = sum(r.get("cost_usd") or 0 for r in probe.records)
    print(f"total cost (tiers with known pricing): ${total:.6f}; ultra cost unknown (no pricing in llm.PRICING)")
    print(json.dumps(caps, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
