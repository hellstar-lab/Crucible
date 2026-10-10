"""P2-1: sequence re-ranking of enum values from logprobs. Offline; the real-data fixture is a recorded nano reply."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import llm

RAW = Path(__file__).resolve().parent.parent / "docs" / "platform-features-raw.jsonl"
ENUM = ["supported", "unsupported", "unclear"]


def _recorded(test, tier):
    for line in RAW.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["test"] == test and r["tier"] == tier:
            return r
    raise LookupError((test, tier))


def test_real_nano_reply_reranks_from_the_greedy_path_to_the_most_probable_value():
    r = _recorded("P1a_schema_neutral", "nano")
    content = r["contents"][0]
    schema = r["params"]["response_format"]["json_schema"]["schema"]
    data, dists, disagree = llm.rerank_enums(json.loads(content), content, r["logprobs"][0], schema)
    d = dists["verdict"]
    assert d["generated"] == "unclear" and data["verdict"] == "unsupported" and disagree and d["changed"]
    assert d["values"]["unsupported"]["prob"] == pytest.approx(0.6959, abs=1e-3)
    assert d["values"]["unclear"]["prob"] == pytest.approx(0.3018, abs=1e-3)
    assert sum(v["prob"] for v in d["values"].values()) + d["unresolved_prob"] == pytest.approx(1.0, abs=2e-3)
    assert "grammar-allowed" in d["note"]


def test_real_ultra_reply_keeps_its_value_and_reports_unresolved_mass():
    r = _recorded("P1a_schema_neutral", "ultra")
    content = r["contents"][0]
    _data, dists, disagree = llm.rerank_enums(json.loads(content), content, r["logprobs"][0], r["params"]["response_format"]["json_schema"]["schema"])
    d = dists["verdict"]
    assert not disagree and d["argmax"] == "unsupported" and d["unresolved_prob"] == pytest.approx(0.06, abs=1e-3)


def tok(token, lp, top=None):
    return {"token": token, "logprob": lp, "top_logprobs": [{"token": t, "logprob": l} for t, l in (top or [(token, lp)])]}


def synthetic(first_top, second_top, second_token):
    """'{"verdict": "' then a value token, then a continuation token, then '"}'."""
    content = [tok('{"', 0.0), tok("verdict", 0.0), tok('":', 0.0), tok(' "', 0.0),
               tok(first_top[0][0], first_top[0][1], first_top), tok(second_token, dict(second_top)[second_token], second_top), tok('"}', 0.0)]
    text = "".join(t["token"] for t in content)
    return {"content": content}, text


SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ENUM}}, "required": ["verdict"], "additionalProperties": False}


def test_a_minus_9999_token_is_a_floor_not_a_dropped_candidate():
    lp, text = synthetic([("un", math.log(0.9)), ("supported", -9999.0)], [("clear", math.log(0.6)), ("supported", math.log(0.4))], "clear")
    _d, dists, _x = llm.rerank_enums(json.loads(text), text, lp, SCHEMA)
    v = dists["verdict"]["values"]
    assert v["supported"]["observed"] and v["supported"]["logprob"] == llm.LOGPROB_FLOOR and v["supported"]["prob"] == 0.0
    assert set(v) == set(ENUM)  # every allowed value is in the distribution


def test_a_value_never_observed_sits_at_the_floor():
    lp, text = synthetic([("un", 0.0)], [("clear", math.log(0.7)), ("supported", math.log(0.3))], "clear")
    _d, dists, _x = llm.rerank_enums(json.loads(text), text, lp, SCHEMA)
    assert dists["verdict"]["values"]["supported"] == {"logprob": llm.LOGPROB_FLOOR, "prob": 0.0, "observed": False}


def test_non_robust_argmax_keeps_the_generated_value():
    # 'un' (generated, 0.5) splits 0.6 clear / 0.4 supported -> unclear 0.30, unsupported 0.20 + 'uns' 0.15 = 0.35;
    # 'u' (0.35, ambiguous, off-path) is unresolved: lead 0.05 < 0.35 -> not robust, generated value kept
    lp, text = synthetic([("un", math.log(0.5)), ("uns", math.log(0.15)), ("u", math.log(0.35))],
                         [("clear", math.log(0.6)), ("supported", math.log(0.4))], "clear")
    data, dists, disagree = llm.rerank_enums(json.loads(text), text, lp, SCHEMA)
    d = dists["verdict"]
    assert d["argmax"] == "unsupported" and not d["robust"] and not disagree and data["verdict"] == "unclear"


def test_nested_enum_fields_in_arrays_are_found():
    schema = {"type": "object", "properties": {"claims": {"type": "array", "items": {"$ref": "#/$defs/C"}}},
              "$defs": {"C": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["metric", "scale"]}}}}}
    assert llm._enum_paths(schema) == {("claims", "*", "kind"): ["metric", "scale"]}
    text = '{"claims": [{"kind": "metric"}, {"kind": "scale"}], "note": "a \\"quoted\\" word"}'
    starts = llm._string_value_starts(text)
    assert [(p, text[o:o + 6]) for p, o in starts] == [(("claims", 0, "kind"), "metric"), (("claims", 1, "kind"), "scale\""),
                                                        (("note",), 'a \\"qu')]


def test_call_requests_logprobs_and_reranks(mocker):
    r = _recorded("P1a_schema_neutral", "nano")
    seen = []

    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r["contents"][0], refusal=None), finish_reason="stop",
                                                        logprobs=r["logprobs"][0])],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    llm._clients.clear()
    mocker.patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key"})
    mocker.patch.object(llm, "OpenAI", side_effect=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    before = llm.COUNTERS["greedy_disagreement"]
    out = llm.call("extract_claims", "p", json_schema=r["params"]["response_format"]["json_schema"]["schema"])
    assert seen[0]["logprobs"] is True and seen[0]["top_logprobs"] == 20
    assert out["data"]["verdict"] == "unsupported" and out["greedy_disagreement"] is True
    assert out["value_distributions"]["verdict"]["generated"] == "unclear"
    assert llm.COUNTERS["greedy_disagreement"] == before + 1
    llm._clients.clear()


def test_no_enum_means_no_logprobs_requested(mocker):
    seen = []

    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"name": "x"}', refusal=None), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    llm._clients.clear()
    mocker.patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key"})
    mocker.patch.object(llm, "OpenAI", side_effect=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    out = llm.call("extract_claims", "p", json_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
                                                       "additionalProperties": False})
    assert "logprobs" not in seen[0] and out["value_distributions"] is None
    llm._clients.clear()
