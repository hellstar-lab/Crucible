from types import SimpleNamespace

import pytest

from backend import llm


def test_models_exact_strings():
    assert llm.MODELS["nano"] == {
        "id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "base_url": "https://api.tokenfactory.nebius.com/v1/",
    }
    assert llm.MODELS["super"] == {
        "id": "nvidia/nemotron-3-super-120b-a12b",
        "base_url": "https://api.tokenfactory.us-central1.nebius.com/v1/",
    }
    assert llm.MODELS["ultra"] == {
        "id": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "base_url": "https://api.tokenfactory.us-central1.nebius.com/v1/",
    }


def test_every_task_has_a_known_tier():
    assert set(llm.TASK_TIER.values()) <= set(llm.MODELS)
    assert len(llm.TASK_TIER) == 15


def test_extract_json_variants():
    assert llm.extract_json('{"a": 1}') == {"a": 1}
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('```\n[1, 2]\n```') == [1, 2]
    assert llm.extract_json('<think>hmm {x}</think>\nSure: {"a": 1} done') == {"a": 1}
    with pytest.raises(llm.LLMJSONError):
        llm.extract_json("no json here")


def test_nano_cost():
    assert llm.estimate_cost("nano", 1_000_000, 1_000_000) == pytest.approx(0.30)
    assert llm.estimate_cost("super", 1_000_000, 1_000_000) == pytest.approx(1.20)
    assert llm.estimate_cost("ultra", 1_000_000, 1_000_000) == pytest.approx(4.00)  # Nebius /models?verbose=true, 2026-10-11


def test_call_routes_and_caches_client(mocker):
    llm._clients.clear()
    created = []

    def fake_openai(**kwargs):
        created.append(kwargs["base_url"])
        reply = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='```json\n{"ok": true}\n```'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply))
        )

    mocker.patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key"})
    mocker.patch.object(llm, "OpenAI", side_effect=fake_openai)

    r1 = llm.call("extract_claims", "p", schema=True)
    r2 = llm.call("classify_file", "p")
    r3 = llm.call("verify_claim", "p")

    assert r1["data"] == {"ok": True} and r1["tier"] == "nano"
    assert r2["data"] is None
    assert r3["model"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    # nano client reused across two calls; ultra gets its own
    assert created == [llm.MODELS["nano"]["base_url"], llm.MODELS["ultra"]["base_url"]]
    llm._clients.clear()


def test_unknown_task():
    with pytest.raises(llm.LLMError):
        llm.call("nope", "p")


def test_library_code_never_loads_dotenv():
    # .env is loaded only at entry points (scripts, app, conftest). Library
    # code reads os.environ so it behaves the same with shell exports or a
    # cloud secret manager.
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent / "backend"
    offenders = [str(p) for p in backend.rglob("*.py") if "dotenv" in p.read_text()]
    assert offenders == []


# --- structured output routing (Phase 4.0): driven by the measured capability matrix ---------------------

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["supported", "unsupported", "insufficient_evidence"]}},
          "required": ["verdict"], "additionalProperties": False}


def _fake_client(mocker, content='{"verdict": "unsupported"}', refusal=None):
    llm._clients.clear()
    seen = []

    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    mocker.patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key"})
    mocker.patch.object(llm, "OpenAI", side_effect=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return seen


def test_committed_capability_file_covers_every_tier():
    caps = llm.load_capabilities()
    assert set(caps) == {"nano", "super", "ultra"}
    for tier in caps.values():  # every measured field is present; unmeasured cost is None, not 0
        assert {"json_object", "json_schema", "strict_enforced", "additional_props_enforced", "required_enforced", "nested_ok",
                "array_of_objects_ok", "median_latency_ms", "cost_per_1k_in", "cost_per_1k_out"} <= set(tier)
    assert caps["ultra"]["cost_per_1k_in"] == pytest.approx(0.001)  # priced since 2026-10-11


def test_enforced_tier_sends_strict_json_schema_and_states_it_in_the_prompt(mocker):
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    seen = _fake_client(mocker)
    r = llm.call("extract_claims", "Judge it.", json_schema=SCHEMA, thinking=False)
    rf = seen[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True and rf["json_schema"]["schema"] == SCHEMA
    assert "insufficient_evidence" in seen[0]["messages"][0]["content"]  # schema also in the prompt
    assert seen[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert r["data"] == {"verdict": "unsupported"} and r["schema_mode"] is True


def test_unenforced_tier_falls_back_to_the_fence_stripping_path(mocker):
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": False}})  # accepted, not enforced
    seen = _fake_client(mocker, content='```json\n{"verdict": "unsupported"}\n```')
    r = llm.call("extract_claims", "Judge it.", json_schema=SCHEMA)
    assert "response_format" not in seen[0] and seen[0]["messages"][0]["content"] == "Judge it."
    assert r["data"] == {"verdict": "unsupported"} and r["schema_mode"] is False


def test_missing_capability_file_means_fallback_everywhere(mocker):
    mocker.patch.object(llm, "_capabilities", {})
    assert not any(llm.schema_enforced(t) for t in llm.MODELS)


def test_refusal_is_checked_before_parsing(mocker):
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    _fake_client(mocker, content="not json at all", refusal="I can't help with that")
    with pytest.raises(llm.LLMRefusalError) as exc:
        llm.call("extract_claims", "p", json_schema=SCHEMA)
    assert exc.value.refusal == "I can't help with that"


def test_pydantic_class_is_accepted_as_json_schema(mocker):
    from pydantic import BaseModel

    class V(BaseModel):
        verdict: str

    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    _fake_client(mocker)
    r = llm.call("extract_claims", "p", json_schema=V)
    assert isinstance(r["data"], V) and r["data"].verdict == "unsupported"


# --- G3 sentinel normalizer (offline) ----------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "This field is intentionally omitted per instruction.",  # measured: nano, T5
    "This field should not be included",                     # measured: ultra, T5
    "",                                                       # measured: super, T5
    "N/A", "n/a", "Not applicable", "No value provided", "Field left blank", "intentionally omitted", "null",
])
def test_sentinels_become_none(value):
    data, n = llm.normalize_sentinels({"name": "cache", "note": value})
    assert data == {"name": "cache", "note": None} and n == 1


@pytest.mark.parametrize("value", [
    "Reduced p95 latency by 40% on the read path",
    "Migrated the field service app to Kubernetes",
    "None of the 312 tests were flaky after the change",
    "Applicable to all EU regions",
    "Python",
])
def test_real_values_are_kept(value):
    assert llm.normalize_sentinels({"text": value}) == ({"text": value}, 0)


def test_sentinels_are_found_in_nested_lists():
    data, n = llm.normalize_sentinels({"claims": [{"text": "Built X", "qualifier": "N/A"}, {"text": "Built Y", "qualifier": "on the read path"}]})
    assert n == 1 and data["claims"][0]["qualifier"] is None and data["claims"][1]["qualifier"] == "on the read path"


# --- G4 refusal detector (offline) -------------------------------------------------------------------------

def test_observed_ultra_refusal_inside_schema_is_detected():
    hit = llm.detect_refusal({"subject": "I cannot fulfill this request", "body": "I'm unable to help with that."})
    assert hit == {"kind": "lexical", "field": "subject", "pattern": "first_person_cannot"}


@pytest.mark.parametrize("data,pattern", [
    ({"text": "I'm sorry, but I can't extract claims from this."}, "first_person_cannot"),
    ({"reason": "As an AI, I do not evaluate people."}, "as_an_ai"),
    ({"reason": "This would be against my guidelines."}, "against_guidelines"),
    ({"x": "Unable to comply with this request."}, "cannot_fulfill"),
])
def test_lexical_refusals(data, pattern):
    hit = llm.detect_refusal(data)
    assert hit and hit["kind"] == "lexical" and hit["pattern"] == pattern


SCHEMA_STRUCT = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["resolved", "cannot_be_disambiguated"]},
    "file_path": {"type": "string"},
    "claims": {"type": "array", "items": {"$ref": "#/$defs/C"}}},
    "$defs": {"C": {"type": "object", "properties": {"text": {"type": "string"}}}}}


@pytest.mark.parametrize("data,pattern,field", [
    ({"decision": "Not possible to decide from the given context"}, "enum_member_is_prose", "decision"),
    ({"file_path": "Unable to determine which file this refers to"}, "path_field_is_prose", "file_path"),
    ({"claims": [{"text": "Built a cache"}, {"text": "Unable to extract a claim from this bullet"}]}, "claim_text_is_inability", "claims[1].text"),
    ({"claims": [{"text": "No verifiable claims can be made here"}]}, "claim_text_is_inability", "claims[0].text"),
])
def test_structural_refusals_follow_the_schema_including_refs(data, pattern, field):
    hit = llm.detect_refusal(data, SCHEMA_STRUCT)
    assert hit == {"kind": "structural", "field": field, "pattern": pattern}


@pytest.mark.parametrize("data", [
    {"claims": [{"text": "Fixed a bug where users cannot log in after a password reset"}]},
    {"claims": [{"text": "Reduced cases where payments could not complete by 30%"}]},
    {"decision": "cannot_be_disambiguated"},  # the abstain VALUE is legitimate, not a refusal
    {"file_path": "src/cache/redis_client.py"},
    {"text": "Declined-payment retries handled via a queue"},
])
def test_resume_like_content_is_not_a_refusal(data):
    assert llm.detect_refusal(data, SCHEMA_STRUCT) is None


def test_fabrication_is_not_a_refusal_and_is_not_caught_here():
    # Measured (T8b): asked to open a URL, all three tiers invented a fetch timestamp instead of refusing.
    # That is a hallucination, not a refusal; detect_refusal cannot and does not claim to catch it.
    assert llm.detect_refusal({"title": "Example Domain", "fetched_at": "2025-06-17T14:32:18Z"}) is None


def test_call_excludes_a_refusal_found_in_content(mocker):
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    _fake_client(mocker, content='{"verdict": "unsupported", "reason": "I cannot fulfill this request"}')
    before = llm.COUNTERS["model_refused"]
    with pytest.raises(llm.LLMRefusalError) as exc:
        llm.call("extract_claims", "p", json_schema=SCHEMA)
    assert exc.value.refusal["kind"] == "lexical" and llm.COUNTERS["model_refused"] == before + 1


def test_call_normalizes_sentinels_and_counts_them(mocker):
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "note": {"type": "string"}}, "required": ["name", "note"],
              "additionalProperties": False}
    mocker.patch.object(llm, "_capabilities", {"nano": {"json_schema": True, "strict_enforced": True}})
    _fake_client(mocker, content='{"name": "cache", "note": "This field is intentionally omitted per instruction."}')
    before = llm.COUNTERS["sentinel_normalized"]
    r = llm.call("extract_claims", "p", json_schema=schema)
    assert r["data"] == {"name": "cache", "note": None} and r["sentinel_normalized"] == 1
    assert llm.COUNTERS["sentinel_normalized"] == before + 1


def test_unpriced_tier_is_capped_by_call_count(mocker):
    mocker.patch.dict(llm.PRICING, {}, clear=True)
    mocker.patch.object(llm, "UNPRICED_TIER_CALL_CAP", 2)
    mocker.patch.object(llm, "_unpriced_calls", llm.Counter())
    _fake_client(mocker)
    llm.call("verify_claim", "p")
    llm.call("verify_claim", "p")
    with pytest.raises(llm.LLMBudgetError):
        llm.call("verify_claim", "p")


def test_prompt_hash_ignores_wrap_untrusted_nonces():
    from backend.safety import wrap_untrusted
    a, b = f"Judge: {wrap_untrusted('Built X')}", f"Judge: {wrap_untrusted('Built X')}"
    assert a != b and llm.prompt_hash(a) == llm.prompt_hash(b)
    assert llm.prompt_hash(f"Judge: {wrap_untrusted('Built Y')}") != llm.prompt_hash(a)


def test_tier_override_routes_to_that_tier(mocker):
    _fake_client(mocker)
    r = llm.call("claim_decomposition", "p", tier="ultra")
    assert r["tier"] == "ultra" and r["model"] == llm.MODELS["ultra"]["id"] and r["prompt_hash"]
    with pytest.raises(llm.LLMError):
        llm.call("claim_decomposition", "p", tier="mega")


def test_n_is_limited_to_the_measured_platform_range(mocker):
    _fake_client(mocker)
    with pytest.raises(llm.LLMError):
        llm.call("claim_disambiguation", "p", n=5, temperature=0.7)  # nano: n <= 3 on non-dedicated models
    with pytest.raises(llm.LLMError):
        llm.call("claim_disambiguation", "p", n=3, temperature=0)  # super/ultra: n must be 1 under greedy sampling


def test_n_returns_every_choice_and_records_a_refusal_per_choice(mocker):
    llm._clients.clear()
    good, bad = '{"verdict": "unsupported"}', '{"verdict": "unsupported", "reason": "I cannot fulfill this request"}'

    def create(**kw):
        assert kw["n"] == 3 and kw["temperature"] == 0.7
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c, refusal=None), finish_reason="stop")
                                        for c in (good, bad, good)], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    mocker.patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key"})
    mocker.patch.object(llm, "OpenAI", side_effect=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    out = llm.call("claim_disambiguation", "p", schema=True, n=3, temperature=0.7)
    assert [c["error"] for c in out["choices"]] == [None, "refusal_detected", None]
    assert out["choices"][0]["data"] == {"verdict": "unsupported"}
    llm._clients.clear()
