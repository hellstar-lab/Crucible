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
    assert cmp_["stranger"]["found_within"][5] is False and cmp_["stranger"]["found_within"][6] is True  # answer at action 7
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


# --- Phase 5 round 4, Part A: the plant-time form check ------------------------------------------------------------

SPACING_FIXTURE = (  # the measured cosmic-ray case: candidates[0] -> candidates[ -1]
    "--- a/backend/evidence/resolve.py\n+++ b/backend/evidence/resolve.py\n@@ -170,3 +170,3 @@\n"
    "         ref_candidates = sorted(set(cands))\n-        ref, scoped = candidates[0]\n+        ref, scoped = candidates[ -1]\n"
    "         return ref\n")


def test_plant_refuses_a_spacing_fingerprint_loudly(repo_and_pool):
    from bugpool.fingerprint import PlantError, check_diff
    from bugpool.session import plant
    repo, pool = repo_and_pool
    with pytest.raises(PlantError, match="findable by its form"):
        check_diff(SPACING_FIXTURE)
    bad = dict(pool["candidates"][0], diff=SPACING_FIXTURE)
    with pytest.raises(PlantError):
        plant(pool, repo, candidate=bad)
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0  # nothing was planted
    with pytest.raises(PlantError):
        Session.start(repo, pool, candidate=bad, python=PY)
    assert _worktrees(repo) == 1  # the session's worktree was destroyed before re-raising


@pytest.mark.parametrize("diff,ok", [
    ("@@ -1 +1 @@\n-if x:\n+if not x:\n", True),
    ("@@ -1 +1 @@\n-if n <= 1:\n+if n < 1:\n", True),
    ("@@ -1 +1 @@\n-x = a + b\n+x = a+b\n", False),                  # whitespace-only hunk
    ("@@ -1,2 +1,2 @@\n-a = 1\n-b = 2\n+a = 2\n+b = 3\n", False),     # two changed lines
    ("@@ -1 +1 @@\n-if resp.status in (403, 429):\n+if resp.status in ( 404, 429):\n", False),
])
def test_check_diff(diff, ok):
    from bugpool.fingerprint import PlantError, check_diff
    if ok:
        check_diff(diff)
    else:
        with pytest.raises(PlantError):
            check_diff(diff)


# --- round 4, Part B: statement granularity and localisation cost --------------------------------------------------

def test_statement_span():
    src = ("def f(a, b):\n    if a < b and (\n            b > 3):\n        x = 1\n        return x\n"
           "    total = (a +\n             b)\n    return total\n")
    assert tools.statement_span(src, 2) == (2, 3)    # an if header spanning two lines
    assert tools.statement_span(src, 3) == (2, 3)
    assert tools.statement_span(src, 4) == (4, 4)    # inside the body: its own statement, not the if
    assert tools.statement_span(src, 7) == (6, 7)    # a multi-line simple statement


def test_localisation_cost():
    log = [{"type": "SEARCH", "files_hit": []}, {"type": "FILE_OPEN", "path": "app/x.py", "ok": True},
           {"type": "FILE_OPEN", "path": "app/y.py", "ok": True}, {"type": "FILE_OPEN", "path": "app/x.py", "ok": True},
           {"type": "ANSWER", "path": "app/x.py", "line": 3}]
    assert tools.localisation_cost(log, "app/x.py", True) == 3      # opened at action 2, answered at action 5
    assert tools.localisation_cost(log, "app/x.py", False) == tools.UNBOUNDED
    found_by_search = [{"type": "SEARCH", "files_hit": ["app/x.py"]}, {"type": "ANSWER"}]
    assert tools.localisation_cost(found_by_search, "app/x.py", True) == 1


def test_the_record_headline_is_localisation_cost(repo_and_pool, tmp_path):
    repo, pool = repo_and_pool
    s = Session.start(repo, pool, seed=0, python=PY)
    c, span = s.cand, s.span
    s.file_open("tests/test_flow.py")
    s.file_open(c["path"])
    s.search("total")
    s.answer(c["path"], span[1])  # the last line of the mutated statement: a statement-level hit
    stranger = tmp_path / "s.json"
    stranger.write_text(json.dumps({"budget": 40, "tier": "super", "results": [
        {"candidate_id": candidate_id(c), "found": False, "localisation_cost": "unbounded", "actions_to_correct": None}]}))
    rec = s.end(None, stranger)
    assert rec["headline"]["localisation_cost"] == {"participant": 2, "stranger": "unbounded"}
    assert rec["headline"]["found"] == {"participant": True, "stranger": False}
    assert rec["outcome"]["granularity"] == "statement" and rec["planted_diff"] == c["diff"]
    assert "distinct_files_opened" in rec["outcome"] and "distinct_files_opened" not in rec["headline"]


# --- round 4, Part C: the session report ---------------------------------------------------------------------------

def _record(repo, pool, tmp_path, stranger_found=False):
    s = Session.start(repo, pool, seed=0, python=PY)
    c = s.cand
    s.search("fee")
    s.file_open(c["path"])
    s.answer(c["path"], c["line"])
    st = tmp_path / "s.json"
    st.write_text(json.dumps({"budget": 40, "tier": "super", "results": [
        {"candidate_id": candidate_id(c), "found": stranger_found, "localisation_cost": 9 if stranger_found else "unbounded",
         "actions_to_correct": 12 if stranger_found else None, "distinct_files_opened": 4}]}))
    return s.end(None, st)


def test_report_shows_the_required_parts_and_nothing_that_reads_as_a_rating(repo_and_pool, tmp_path):
    import re
    from bugpool.report import render_session
    from backend.grounding.html import assert_traceable
    rec = json.loads(json.dumps(_record(*repo_and_pool, tmp_path)))  # as written to disk
    page = render_session(rec)
    assert assert_traceable(page, rec) > 10
    ids = re.findall(r'<section id="([\w-]+)"', page)
    assert ids == ["candidate", "outcome", "timeline", "diff", "recorded"]
    assert rec["candidate"]["module"] in page and rec["candidate"]["operator"] in page
    assert html_escape(rec["planted_diff"].splitlines()[-1]) in page and "Shown only after the session ended" in page
    assert page.count('class="kind"') == len(rec["actions"])  # every action is on the timeline
    outcome = page.split('id="outcome"')[1].split("</section>")[0]
    assert "unbounded" in outcome and "found" in outcome
    assert "elapsed" not in outcome.lower() and "seconds" not in outcome.lower()  # time is never beside the stranger's
    text = re.sub(r"<[^>]+>", " ", page.split("</header>")[1]).lower()
    for banned in ("score", "rank", "hire", "percentile", "faster", "slower", "beat", "rating", "grade"):
        assert banned not in text.replace("not a score", ""), banned


def test_report_for_a_refused_session(repo_and_pool):
    from bugpool.report import render_session
    page = render_session({"refused": True, "message": INSUFFICIENT, "reason": "the repository has no tests", "planted": False})
    assert INSUFFICIENT in page and "Nothing was planted" in page


def html_escape(s):
    import html
    return html.escape(s)


# --- unseen discipline ------------------------------------------------------------------------------------------------

def test_a_burned_line_is_never_pooled_and_a_session_burns_what_it_planted(tmp_path):
    from bugpool import burned
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=5, max_per_killing_test=5,
                         log=lambda *_: None)
    assert pool["status"] == "ok" and pool["unseen_discipline"]["burned_lines"] == 0
    first = pool["candidates"][0]
    s = Session.start(repo, pool, candidate=first, python=PY)
    rec = s.end()
    assert rec["burned"] is True
    reg = burned.Registry(repo / burned.DEFAULT)
    assert reg.burned(first["path"], first["line"]) and candidate_id(first) in reg.ids
    again = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=5, max_per_killing_test=5,
                          log=lambda *_: None)
    assert (first["path"], first["line"]) not in {(c["path"], c["line"]) for c in again["candidates"]}
    assert again["unseen_discipline"]["excluded_candidates"] >= 1
    # the registry lives in a dot-directory: the session tools can neither list nor open it
    assert not any(f.startswith(".bugpool") for f in tools.tree(repo))
    assert tools.file_open(repo, ".bugpool/burned.json")[0].startswith("error")


def test_burn_text_resolves_bare_names_to_every_matching_file(tmp_path):
    from bugpool import burned
    repo = make_repo(tmp_path, dict(APP, **{"app/sub/ledger.py": "x = 1\ny = 2\n"}))
    reg = burned.Registry(tmp_path / "r.json")
    assert burned.burn_text(reg, repo, "see ledger.py:2 and app/report.py:5", "doc", source="app") == 3
    assert reg.burned("app/ledger.py", 2) and reg.burned("app/sub/ledger.py", 2) and reg.burned("app/report.py", 5)
