"""The timed session and the shared tools. Offline; needs the spike venv (cosmic-ray, pytest-cov) for the pool."""

from __future__ import annotations

import json
import subprocess

import pytest

pytest.importorskip("cosmic_ray")

from bugpool import INSUFFICIENT, tools  # noqa: E402
from bugpool.pool import generate_pool  # noqa: E402
from bugpool.session import Session, candidate_id  # noqa: E402

from test_bugpool import APP, FLOW, ME, PY, make_repo  # noqa: E402

SECRET = "TOPSECRET-do-not-show"


@pytest.fixture(scope="module")
def repo_and_pool(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sess")
    files = dict(APP, **{"tests/test_flow.py": FLOW, ".env.example": f"KEY={SECRET}\n", ".hidden/notes.txt": SECRET})
    repo = make_repo(tmp, files)
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=3, log=lambda *_: None)
    assert pool["status"] == "ok"
    return repo, pool


def _worktrees(repo):
    return subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.count("\n")


def _keys(x):
    if isinstance(x, dict):
        return {str(k) for k in x} | {k for v in x.values() for k in _keys(v)}
    if isinstance(x, list):
        return {k for v in x for k in _keys(v)}
    return set()


def test_an_empty_pool_is_refused_and_nothing_is_planted(repo_and_pool, tmp_path):
    repo, _pool = repo_and_pool
    empty = {"status": "empty", "candidates": [], "reason": "the repository has no tests", "head_sha": "x"}
    s = Session.start(repo, empty)
    assert s.refused == INSUFFICIENT and s.wt is None and _worktrees(repo) == 1
    rec = s.end(tmp_path / "r.json")
    assert rec == {"refused": True, "message": INSUFFICIENT, "reason": "the repository has no tests", "planted": False}


def test_the_participant_sees_the_repo_and_the_failing_test_and_nothing_else(repo_and_pool):
    repo, pool = repo_and_pool
    s = Session.start(repo, pool, seed=0, python=PY)
    try:
        v = s.view()
        assert set(v) == {"files", "failing_test", "failing_output", "budget"}
        assert v["failing_test"] == s.cand["killing_test"] and ("failed" in v["failing_output"] or "FAILED" in v["failing_output"])
        assert not any(f.startswith(".") or "/." in f for f in v["files"])  # dotfiles are not part of the view
        assert s.cand["diff"] not in json.dumps(v) and s.cand["mutated_line"].strip() not in v["failing_output"]
        # the sandbox: no dotfile can be opened or searched
        assert s.file_open(".env.example").startswith("error") and s.file_open("../../etc/passwd").startswith("error")
        assert SECRET not in s.search("TOPSECRET") and s.search("x", ".hidden").startswith("error")
    finally:
        s.end()
    assert _worktrees(repo) == 1 and subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0


def test_actions_are_logged_and_a_correct_answer_is_recorded(repo_and_pool, tmp_path):
    repo, pool = repo_and_pool
    s = Session.start(repo, pool, seed=0, python=PY)
    c = s.cand
    s.search("def ")
    s.file_open(c["path"])
    s.test_run(c["killing_test"])
    s.answer(c["path"], c["line"])
    stranger = tmp_path / "stranger.json"
    stranger.write_text(json.dumps({"budget": 40, "tier": "super", "results": [
        {"candidate_id": candidate_id(c), "actions_to_correct": 7, "distinct_files_opened": 3}]}))
    rec = s.end(tmp_path / "r.json", stranger)
    assert [a["type"] for a in rec["actions"]] == ["SEARCH", "FILE_OPEN", "TEST_RUN", "ANSWER"]
    assert all({"t", "at", "type"} <= set(a) for a in rec["actions"]) and set(rec["action_types"]) == set(tools.ACTIONS)
    o = rec["outcome"]
    assert o["status"] == "found" and o["correct"] and o["actions_to_correct"] == 4 and o["distinct_files_opened"] == 1
    cmp_ = rec["comparison"]
    assert cmp_["participant_actions_to_find"] == 4 and cmp_["stranger"]["actions_to_correct"] == 7
    assert cmp_["stranger"]["found_within"][6] is False and cmp_["stranger"]["found_within"][7] is True
    assert rec["candidate"]["id"] == candidate_id(c) and rec["candidate"]["operator"] == c["operator"]
    for banned in ("score", "rank", "ranking", "hire", "grade", "percentile", "faster", "speedup"):
        assert not any(banned in k.lower() for k in _keys(rec)), banned
    assert json.loads((tmp_path / "r.json").read_text()) == json.loads(json.dumps(rec))
    assert _worktrees(repo) == 1 and subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0


def test_running_out_of_budget_is_a_recorded_outcome_not_a_failure(repo_and_pool):
    repo, pool = repo_and_pool
    s = Session.start(repo, pool, seed=0, budget=2, python=PY)
    s.search("total")
    s.search("amount")
    assert s.search("fee") == "the session has ended"
    rec = s.end()
    assert rec["outcome"]["status"] == "not_found_within_budget" and rec["outcome"]["submitted"] is None
    assert rec["outcome"]["actions_used"] == 2 and rec["comparison"]["participant_actions_to_find"] is None
