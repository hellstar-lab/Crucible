"""Phase 4G: question generation, offline (mocked verdicts, scripted model, no API calls)."""

from __future__ import annotations

import pytest

from backend import llm, questions, retrieval
from test_retrieval import build_scenarios, claim, view

import make_git_fixtures as M


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return build_scenarios(tmp_path_factory.mktemp("questions"))


class Phraser:
    """Scripted phrasing model: ``answers`` maps claim_id -> question text (or a default per shape)."""

    def __init__(self, answers=None):
        self.answers, self.calls, self.prompts = answers or {}, 0, []

    def __call__(self, task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None,
                 json_schema=None, tier=None, n=1):
        assert task == "question_generation" and n == 1 and thinking is False
        self.calls += 1
        self.prompts.append(prompt)
        ids = json_schema["properties"]["questions"]["items"]["properties"]["id"]["enum"]
        out = []
        for qid, spec in zip(ids, self.specs):
            out.append({"id": qid, "question": self.answers.get(spec["claim_id"], DEFAULT[spec["shape"]](spec))})
        return {"data": {"questions": out}, "cost_usd": 0.0004}


DEFAULT = {
    "code_decision": lambda s: (f"In {s['anchor'].path}:{s['anchor'].start_line}-{s['anchor'].end_line}, {s['anchor'].function} "
                                "returns early. What made that the right choice here?"),
    "role": lambda s: f"Tell me about your role on {s['anchor'].artifact}.",
    "where": lambda s: "Where did that work happen?",
    "walkthrough": lambda s: "Walk me through that work.",
    "conversation": lambda s: "Tell me more about that.",
}


@pytest.fixture
def phraser(monkeypatch):
    p = Phraser()
    real_select = questions.select

    def select(*a, **kw):
        specs, skipped = real_select(*a, **kw)
        p.specs = specs  # ids q0.. follow the ranked specs; the cap keeps a prefix
        return specs, skipped

    monkeypatch.setattr(questions, "select", select)
    monkeypatch.setattr(llm, "call", p)
    return p


def _row(cid, text, kind, verdict, citation=None, target=None):
    return {"claim_id": cid, "claim_text": text, "claim_kind": kind, "verdict": verdict, "citation": citation, "scope_target": target}


def _setup(scenarios):
    v = view(scenarios, "clean_small")
    own = claim("Built the cache client", "ownership", "c1")
    redis = claim("Built a caching layer with Redis", "technology_use", "c2")
    ledger = claim("Designed the ledger reconciliation module", "ownership", "c0")
    kafka = claim("Used Kafka for event streaming", "technology_use", "c3")
    sets = [retrieval.retrieve(c, [v]) for c in (own, redis, ledger, kafka)]
    rows = [
        _row("c0", ledger["text"], "ownership", "contradicted", {"repo": v.full_name, "path": "src/billing/ledger.py", "start_line": 1, "end_line": 6},
             "ledger reconciliation"),
        _row("c1", own["text"], "ownership", "supported", {"repo": v.full_name, "path": "src/cache.py", "start_line": None, "end_line": None},
             "cache"),
        _row("c2", redis["text"], "technology_use", "supported", {"repo": v.full_name, "path": "src/cache.py", "start_line": 1, "end_line": 1}),
        _row("c3", kafka["text"], "technology_use", "no_evidence"),
        _row("c4", "Reduced latency by 40%", "metric", "no_evidence"),
        _row("c5", "Maintained the API layer", "ownership", "inconclusive"),
        _row("c6", "Led a team of 6 engineers", "role_or_team", "unverifiable"),
    ]
    return {"rows": rows}, sets, [v]


# --- ranking, cap, selection ----------------------------------------------------------------------------------------

def test_ranking_is_deterministic_and_follows_the_order(scenarios):
    report, sets, views = _setup(scenarios)
    a, skipped = questions.select(report, sets, views)
    b, _ = questions.select(report, sets, views)
    assert [s["claim_id"] for s in a] == [s["claim_id"] for s in b]
    verdicts = [s["verdict"] for s in a]
    assert verdicts[0] == "contradicted" and verdicts.index("no_evidence") > max(i for i, v in enumerate(verdicts) if v == "supported")
    assert [s["claim_id"] for s in a if s["verdict"] == "no_evidence"] == ["c3", "c4"]  # technology claims first
    assert verdicts[-1] == "inconclusive" and "c6" in [x["claim_id"] for x in skipped]  # unverifiable needs the flag
    sup = [s for s in a if s["verdict"] == "supported"]
    assert all(s["anchor"].kind == "function" and s["anchor"].path == "src/cache.py" for s in sup)
    assert len({(s["anchor"].path, s["anchor"].start_line) for s in sup}) == len(sup)  # no function anchors two questions
    scores = [-s["rank_key"][1] for s in sup]
    assert scores == sorted(scores, reverse=True)  # CCN x authored ratio, highest first


def test_contradicted_anchor_carries_no_code(scenarios):
    report, sets, views = _setup(scenarios)
    c0 = next(s for s in questions.select(report, sets, views)[0] if s["claim_id"] == "c0")
    assert c0["anchor"].kind == "claim" and c0["anchor"].path is None and c0["body"] is None
    assert c0["anchor"].artifact == "the ledger reconciliation module"  # the claim's own words, minus its verb


def test_unverifiable_included_only_with_the_flag(scenarios):
    report, sets, views = _setup(scenarios)
    specs, _ = questions.select(report, sets, views, include_unverifiable=True)
    assert specs[-1]["claim_id"] == "c6" and specs[-1]["shape"] == "conversation"


def test_cap_drops_the_lowest_ranked_and_records_the_count(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    full, _ = questions.select(report, sets, views)
    qs = questions.generate(report, sets, views, max_questions=2)
    assert len(qs.questions) == 2 and qs.dropped_by_cap == len(full) - 2
    assert qs.dropped_claim_ids == [s["claim_id"] for s in full[2:]] and phraser.calls == 1


def test_the_model_never_sees_a_verdict(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    questions.generate(report, sets, views)
    p = phraser.prompts[0].lower()
    for w in ("contradicted", "supported", "no_evidence", "inconclusive", "verdict"):
        assert w not in p
    assert "reconcile_ledger" not in p and "src/billing/ledger.py" not in p  # unauthored code is never sent


def test_neutral_questions_pass_every_rule(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    qs = questions.generate(report, sets, views)
    assert not qs.rejected, qs.rejected
    assert qs.questions[0].text == "Tell me about your role on the ledger reconciliation module."
    assert [q.rank for q in qs.questions] == list(range(1, len(qs.questions) + 1))


# --- the four hard rules --------------------------------------------------------------------------------------------

def test_r1_a_question_citing_unauthored_code_is_rejected(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    phraser.answers = {"c1": "In src/billing/ledger.py, how does reconcile_ledger handle a mismatch?",
                       "c2": "How does reconcile_ledger interact with the Redis cache?"}
    qs = questions.generate(report, sets, views)
    rej = {r["claim_id"]: r["rule"] for r in qs.rejected}
    assert rej == {"c1": "R1", "c2": "R1"}


def test_r2_accusatory_phrasing_on_a_contradicted_row_is_rejected(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    for text in ("Why did you claim you designed the ledger reconciliation module?",
                 "You claim the ledger reconciliation module is yours. Did you really write it?",
                 "Who actually designed the ledger reconciliation?", "Allegedly you built the ledger reconciliation; tell me more."):
        phraser.answers = {"c0": text}
        qs = questions.generate(report, sets, views)
        assert [r["rule"] for r in qs.rejected if r["claim_id"] == "c0"] == ["R2"], text


def test_r3_a_question_repeating_its_verdict_is_rejected(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    phraser.answers = {"c0": "Your ledger reconciliation claim was contradicted. Tell me about your role on it.",
                       "c3": "We found no evidence of Kafka. Where did that work happen?",
                       "c1": "Blame shows you wrote this cache code. What made that choice?"}
    qs = questions.generate(report, sets, views)
    assert {r["claim_id"]: r["rule"] for r in qs.rejected} == {"c0": "R3", "c3": "R3", "c1": "R3"}


def test_r4_a_technology_absent_from_the_anchor_is_rejected(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    phraser.answers = {"c1": "Why does this cache client use Memcached instead of Kafka for invalidation?",
                       "c3": "Where did the 12 Kafka clusters live?"}
    qs = questions.generate(report, sets, views)
    rej = {r["claim_id"]: r for r in qs.rejected}
    assert rej["c1"]["rule"] == "R4" and rej["c3"]["rule"] == "R4" and "12" in rej["c3"]["detail"]


# --- G6: per student ------------------------------------------------------------------------------------------------

def test_per_student_sheet_holds_only_that_students_code(scenarios, phraser):
    report, sets, views = _setup(scenarios)
    bob = M.BOB[1]
    fns = questions.authored_functions(views[0], bob)
    assert fns and all(f.path in ("src/billing/ledger.py",) or f.path.startswith(".github/") for f in fns)
    report["rows"].append(_row("c7", "Wrote the ledger totals", "ownership", "supported",
                               {"repo": views[0].full_name, "path": "src/billing/ledger.py", "start_line": None, "end_line": None}))
    qs = questions.generate(report, sets, views, person=bob)
    assert [q.claim_id for q in qs.questions] == ["c7"] and qs.person == bob
    assert all(q.anchor.kind == "function" and q.anchor.path == "src/billing/ledger.py" for q in qs.questions)
    assert {s["claim_id"] for s in qs.skipped} >= {"c0", "c1", "c2"}  # the candidate's code is not Bob's


def test_questions_render_traced_and_neutral(scenarios, phraser):
    import copy, json, re
    from pathlib import Path
    from backend.grounding.html import assert_traceable, render_html
    report, sets, views = _setup(scenarios)
    qs = questions.generate(report, sets, views)
    data = json.loads((Path(__file__).parent / "fixtures" / "reports" / "degraded.json").read_text())
    data = copy.deepcopy(data)
    data["questions"] = json.loads(qs.model_dump_json())
    page = render_html(data)
    assert assert_traceable(page, data) > 50
    ids = re.findall(r'<section id="([\w-]+)"', page)
    assert ids.index("questions") == ids.index("claims") + 1
    assert "Tell me about your role on the ledger reconciliation module." in page and "for the reviewer" in page



@pytest.mark.parametrize("line,names", [("import redis", {"redis"}), ("from rapidfuzz import fuzz, process as p", {"fuzz", "p"}),
                                        ("import numpy.linalg as la", {"la"}), ("from pydantic import (BaseModel, Field)", {"BaseModel", "Field"}),
                                        ("import React, { useState } from 'react';", {"React"}), ("const ioredis = require('ioredis')", {"ioredis"})])
def test_bound_names(line, names):
    assert names <= questions.bound_names(line)


def test_technology_questions_anchor_only_functions_that_use_it(scenarios):
    report, sets, views = _setup(scenarios)
    c2 = next(s for s in questions.select(report, sets, views)[0] if s["claim_id"] == "c2")
    assert "redis" in c2["body"]  # the anchor function references what the import binds


def test_an_unconfirmed_contradiction_is_asked_like_one_and_never_capped(scenarios, phraser):
    """Measured (live run 3): Ultra did not confirm the unicode-module contradiction, the row became inconclusive,
    ranked with ordinary inconclusive rows, and the cap removed it: neither alleged nor asked."""
    report, sets, views = _setup(scenarios)
    report["rows"][0] = {**report["rows"][0], "verdict": "inconclusive", "matrix_row": "contradiction:not_confirmed"}
    specs, _ = questions.select(report, sets, views)
    c0 = next(s for s in specs if s["claim_id"] == "c0")
    assert c0["shape"] == "role" and c0["anchor"].kind == "claim" and c0["rank_key"][0] == 1 and specs[0] is c0
    qs = questions.generate(report, sets, views, max_questions=1)
    assert [q.claim_id for q in qs.questions] == ["c0"] and qs.questions[0].verdict == "inconclusive"  # reviewer still sees it
    qs0 = questions.generate(report, sets, views, max_questions=0)
    assert [q.claim_id for q in qs0.questions] == ["c0"]  # group 1 is always included, even past the cap
