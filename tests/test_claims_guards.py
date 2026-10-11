"""T1-T4 guards: stage partitions, Stage 3 tier, rubric-leak and ungrounded-qualifier checks. Offline."""

from __future__ import annotations

import json

import pytest

from backend import llm
from backend.claims import config, guards, pipeline, stages

from test_claims_pipeline import FakeLLM, J, codes


@pytest.fixture
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(llm, "call", f)
    return f


# --- T1 / T2 -------------------------------------------------------------------------------------------------

def test_no_stage_enum_has_an_abstain_value():
    assert stages.DECOMPOSITION_SCHEMA["properties"]["verdict"]["enum"] == ["decomposed", "no_verifiable_claims"]
    for sch in stages.SCHEMAS.values():
        assert "unclear" not in sch["properties"]["verdict"]["enum"]


def test_stage3_defaults_to_ultra_and_escalation_is_off(fake):
    assert config.STAGE3_TIER == "ultra" and llm.TASK_TIER["claim_disambiguation"] == "ultra"
    pipeline.extract_from_text("• Rewrote it in Go", decomposition_tier="nano")
    assert [tier for task, tier, _p in fake.prompts if task == "claim_disambiguation"] == ["ultra"]


def test_reason_first_is_the_default_and_verdict_first_remains_for_ab():
    from backend.claims import stages
    assert config.REASON_FIRST is True
    assert list(stages.schema_for("disambiguation")["properties"])[:2] == ["reason", "verdict"]
    assert list(stages.schema_for("disambiguation", reason_first=False)["properties"])[:2] == ["verdict", "reason"]


# --- T3: rubric leak -----------------------------------------------------------------------------------------

def test_observed_leak_kafka_event_bus_for_a_role_line():
    hit = guards.rubric_leak("Engineered a Kafka-based event bus handling 5k events/s", "Engineer, Contoso Freight Jul 2018 - Feb 2021")
    assert hit and hit[0] == "Built a Kafka-based event bus handling 5k events/s"


def test_observed_leak_command_line_tool_as_a_guessed_referent():
    # caught by nothing else: no number, no named entity
    claim = "Improved the command-line tool for log search by 40%"
    source = "Improved it by 40%\nHeadings: EXPERIENCE > Engineer, Contoso Freight Jul 2018 - Feb 2021"
    hit = guards.rubric_leak(claim, source)
    assert hit and hit[0] == "Wrote a command-line tool for log search"
    from backend.claims import support
    assert not support.check(claim, source).hard_fail  # the typed check alone misses it


def test_control_common_words_shared_with_an_example_do_not_fire():
    assert guards.rubric_leak("Built an event bus for order processing", "Built an event bus for order processing") is None
    assert guards.rubric_leak("Moved logging to Datadog", "Moved logging to Datadog") is None  # shares "moved logging"
    assert guards.rubric_leak("Cut build time by 50% with Bazel", "Cut build time by 50% with Bazel") is None  # in the source


def test_a_leak_present_in_the_source_is_not_a_leak():
    src = "Built a Kafka-based event bus handling 5k events/s for the orders team"
    assert guards.rubric_leak("Built a Kafka-based event bus handling 5k events/s", src) is None


def test_pipeline_excludes_and_counts_a_rubric_leak(fake):
    unit = "Engineer, Contoso Freight Jul 2018 - Feb 2021"
    fake.script[("claim_decomposition", unit)] = J("decomposed", claims=[{"text": "Engineered a Kafka-based event bus handling 5k events/s", "qualifiers": []}])
    out = pipeline.extract_from_text(unit, decomposition_tier="nano")
    assert codes(out) == {"rubric_leak"} and out.counters["rubric_leak"] == 1 and out.claims == []


# --- T4: ungrounded qualifier --------------------------------------------------------------------------------

def test_observed_proficient_from_a_plain_skills_list():
    assert guards.ungrounded_qualifiers("Proficient in Python", "Python, Go, PostgreSQL") == ["proficient"]


@pytest.mark.parametrize("claim", ["Expert in Kubernetes", "Has deep experience with Go", "Advanced SQL skills",
                                   "Seasoned backend engineer", "Highly skilled in Rust", "Strong knowledge of AWS"])
def test_lexicon_words_absent_from_the_source_fire(claim):
    assert guards.ungrounded_qualifiers(claim, "Kubernetes, Go, SQL, Rust, AWS, backend engineer")


def test_control_a_source_that_says_proficient_passes():
    assert guards.ungrounded_qualifiers("Proficient in Python", "Skills: proficient in Python and Go") == []
    assert guards.ungrounded_qualifiers("Senior Engineer at Northwind Payments", "Senior Engineer, Northwind Payments Mar 2021 - Present") == []
    assert guards.ungrounded_qualifiers("Expertise in Go", "Areas of expert knowledge: Go") == []  # stems match


def test_pipeline_excludes_and_counts_an_ungrounded_qualifier(fake):
    unit = "Python, Go, PostgreSQL"
    fake.script[("claim_decomposition", unit)] = J("decomposed", claims=[{"text": "Proficient in Python", "qualifiers": []}])
    # under a non-skills heading the unit takes the model path, where the guard applies (under SKILLS, J1 splits the
    # list in code and no model ever sees it: see test_claims_skills.py)
    out = pipeline.extract_from_text(unit, headings=["SUMMARY"], decomposition_tier="nano")
    assert codes(out) == {"ungrounded_qualifier"} and out.counters["ungrounded_qualifier"] == 1


# --- escalation ----------------------------------------------------------------------------------------------

class TierFake(FakeLLM):
    """Stage 3 answers depend on the tier: super guesses a rubric referent, ultra refuses correctly."""

    def __call__(self, task, prompt, *a, tier=None, n=1, **kw):
        if task == "claim_disambiguation":
            self.prompts.append((task, tier, prompt))
            ans = (J("resolved", clarified_text="Improved the command-line tool for log search by 40%", failed_referent=None)
                   if tier == "super" else J("cannot_be_disambiguated", clarified_text=None, failed_referent="it"))
            out = {"text": json.dumps(ans), "data": ans, "cost_usd": 0.0001, "prompt_hash": "h", "sentinel_normalized": 0}
            if n > 1:
                out["choices"] = [{"text": json.dumps(ans), "data": ans, "error": None}] * n
            return out
        return super().__call__(task, prompt, *a, tier=tier, n=n, **kw)


def test_escalation_off_excludes_the_guess(monkeypatch):
    f = TierFake()
    monkeypatch.setattr(llm, "call", f)
    out = pipeline.extract_from_text("• Improved it by 40%", decomposition_tier="nano", disambiguation_tier="super")
    assert codes(out) == {"rubric_leak"} and out.counters["stage3_escalations"] == 0


def test_escalation_on_asks_ultra_once(monkeypatch):
    f = TierFake()
    monkeypatch.setattr(llm, "call", f)
    out = pipeline.extract_from_text("• Improved it by 40%", decomposition_tier="nano", disambiguation_tier="super",
                                     stage3_escalation="ultra")
    assert [t for task, t, _p in f.prompts if task == "claim_disambiguation"] == ["super", "ultra"]
    assert codes(out) == {"cannot_be_disambiguated"} and out.counters["stage3_escalations"] == 1


# --- T6: the eval runner reports k of n -----------------------------------------------------------------------

def test_eval_runner_reports_k_of_n_and_distinct_outputs(fake, monkeypatch, capsys):
    import importlib.util
    import itertools
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("eval_extraction", Path(__file__).resolve().parent.parent / "scripts" / "eval_extraction.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    unit = "Reduced p95 latency by 70% on the read path"
    flip = itertools.cycle([[{"text": unit, "qualifiers": ["on the read path"]}], [{"text": "Reduced p95 latency by 70%", "qualifiers": []}]])
    orig = fake.default
    fake.default = lambda task, u: J("decomposed", claims=next(flip)) if task == "claim_decomposition" else orig(task, u)
    monkeypatch.setattr("sys.argv", ["eval_extraction.py", "--rows", "w4_load_bearing_qualifier", "--runs", "4"])
    assert ev.main() == 0
    out = capsys.readouterr().out
    assert "w4_load_bearing_qualifier    2 of 4 passed  (2 distinct outcomes)" in out
    assert "x2 PASS" in out and "x2 FAIL" in out



# --- P2-3 / P2-6: ensemble unanimity and semantic entropy -----------------------------------------------------

from backend.claims import ensemble  # noqa: E402


def S(verdict, clarified=None, error=None):
    return {"verdict": verdict, "clarified_text": clarified, "failed_referent": "it" if verdict == "cannot_be_disambiguated" else None,
            "reason": "r", "error": error}


def test_unanimous_resolution_is_accepted():
    d = ensemble.decide([S("resolved", "Rewrote the job scheduler in Rust")] * 3, "Rewrote it in Rust")
    assert d["verdict"] == "resolved" and d["unanimous"] and d["clarified_text"] == "Rewrote the job scheduler in Rust"


def test_two_of_three_is_not_enough():
    samples = [S("resolved", "Improved the role by 40%"), S("resolved", "Improved the role by 40%"), S("cannot_be_disambiguated")]
    d = ensemble.decide(samples, "Improved it by 40%")
    assert d["verdict"] == "cannot_be_disambiguated" and d["reason"] == "resolutions disagreed" and not d["unanimous"]


def test_different_referents_disagree_even_if_nli_calls_them_equivalent():
    samples = [S("resolved", "Improved the role by 40%"), S("resolved", "Improved the experience by 40%"), S("resolved", "Improved the role by 40%")]
    d = ensemble.decide(samples, "Improved it by 40%", equivalent=lambda a, b: True)  # a maximally lenient NLI
    assert d["verdict"] == "cannot_be_disambiguated" and d["reason"] == "resolutions disagreed"


def test_same_referent_worded_differently_agrees_with_nli():
    samples = [S("resolved", "Rewrote the job scheduler in Rust"), S("resolved", "Rewrote the job scheduler using Rust"),
               S("resolved", "Rewrote the job scheduler in Rust.")]
    assert ensemble.decide(samples, "Rewrote it in Rust", equivalent=lambda a, b: True)["verdict"] == "resolved"
    assert ensemble.decide(samples, "Rewrote it in Rust", equivalent=None)["verdict"] == "cannot_be_disambiguated"  # no NLI: strict


def test_unanimous_refusal_and_errored_samples():
    assert ensemble.decide([S("cannot_be_disambiguated")] * 3, "Improved it by 40%")["unanimous"]
    d = ensemble.decide([S("resolved", "x"), S("resolved", "x"), S(None, error="unparseable")], "u")
    assert d["verdict"] == "cannot_be_disambiguated" and not d["unanimous"]


@pytest.mark.parametrize("sizes,expected", [([3], 0.0), ([2, 1], 0.6365), ([1, 1, 1], 1.0986)])
def test_semantic_entropy_values_for_n3(sizes, expected):
    cl, i = [], 0
    for k in sizes:
        cl.append(list(range(i, i + k)))
        i += k
    assert ensemble.semantic_entropy(cl) == pytest.approx(expected, abs=1e-3)


def test_clusters_by_bidirectional_entailment_with_refusals_apart():
    samples = [S("resolved", "a"), S("resolved", "b"), S("cannot_be_disambiguated")]
    assert ensemble.clusters(samples, equivalent=lambda x, y: True) == [[0, 1], [2]]
    assert ensemble.clusters(samples, equivalent=lambda x, y: False) == [[0], [1], [2]]


def test_stage3_ensemble_disagreement_refuses_and_records_all_samples(monkeypatch):
    f = FakeLLM()
    f.script[("claim_disambiguation", "Improved it by 40%")] = [J("resolved", clarified_text="Improved the role by 40%", failed_referent=None),
                                                                 J("cannot_be_disambiguated", clarified_text=None, failed_referent="it"),
                                                                 J("resolved", clarified_text="Improved the role by 40%", failed_referent=None)]
    monkeypatch.setattr(llm, "call", f)
    out = pipeline.extract_from_text("• Improved it by 40%", decomposition_tier="nano")
    assert codes(out) == {"cannot_be_disambiguated"} and out.counters["stage3_ensemble_disagreements"] == 1
    d3 = [d for d in out.units[0].decisions if d.stage == "disambiguation"][0]
    assert len(d3.samples) == 3 and "resolutions disagreed" in out.excluded[0].detail
