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
    assert len(llm.TASK_TIER) == 12


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
    assert llm.estimate_cost("ultra", 10, 10) is None  # pricing not yet known


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
