"""Claim-targeted selection: the bug is planted where the résumé made a claim, strongest claim first; the claim text
never reaches the hunt (the Shield rule). Needs the spike venv (see test_bugpool.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cosmic_ray")
pytest.importorskip("pytest_cov")

from bugpool import claims  # noqa: E402
from bugpool.pool import generate_pool  # noqa: E402
from bugpool.report import render_session  # noqa: E402
from bugpool.session import Session, ShieldError, leaked_claims  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_bugpool import APP, FLOW, ME, PY, make_repo  # noqa: E402

CLAIM = "Designed the month-end ledger close flow end to end"  # long enough to identify a claim; not in the repo


def record(*rows):
    return {"header": {"analysed": [{"repo": "me/app", "head_sha": "abc"}]},
            "rows": [{"claim_id": cid, "claim_text": text, "claim_kind": kind, "verdict": verdict,
                      "citation": {"repo": "me/app", "path": path, "start_line": a, "end_line": b} if path else None}
                     for cid, text, kind, verdict, path, a, b in rows]}


# the fixture's clean candidates are all in app/ledger.py: fee (lines 1-6) and balance (lines 9-15)
REC = record(("c0", CLAIM, "ownership", "supported", "app/ledger.py", 9, 15),
             ("c1", "Python", "technology_use", "supported", "app/report.py", None, None),
             ("c2", "Wrote the fee rules", "ownership", "inconclusive", "app/ledger.py", 1, 6),
             ("c3", "Led a team of 4", "role_or_team", "unverifiable", None, None, None))


@pytest.fixture(scope="module")
def repo_and_pool(tmp_path_factory):
    repo = make_repo(tmp_path_factory.mktemp("claims"), dict(APP, **{"tests/test_flow.py": FLOW}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=10, max_per_killing_test=10,
                         max_per_module=10, log=lambda *_: None)
    assert pool["status"] == "ok" and {c["path"] for c in pool["candidates"]} == {"app/ledger.py"}
    return repo, pool


def test_targeting_ranks_supported_then_inconclusive_then_uncited():
    cites = claims.citations(REC)
    assert all("claim_text" not in c for c in cites)  # citations carry no text
    t = claims.target_for("app/ledger.py", 13, cites)
    assert t["cited"] and t["verdict"] == "supported" and t["rank"] == 0 and t["span_cited"] and t["claim_id"] == "c0"
    fee = claims.target_for("app/ledger.py", 3, cites)
    # the supported claim outranks the inconclusive one even outside its span: the verdict comes first
    assert fee["verdict"] == "supported" and fee["claim_id"] == "c0" and not fee["span_cited"]
    assert [c["claim_id"] for c in fee["claims"]] == ["c0", "c2"]
    assert claims.target_for("app/report.py", 6, cites)["claim_kind"] == "technology_use"
    assert claims.target_for("app/other.py", 1, cites) == {"cited": False, "rank": claims.UNCITED, "verdict": None, "claims": []}
    inc = record(("c2", "Wrote the fee rules", "ownership", "inconclusive", "app/ledger.py", 1, 6))
    assert claims.target_for("app/ledger.py", 3, claims.citations(inc))["rank"] == 1
    # within supported: inside the cited span first; an ownership claim before a technology_use citation
    assert t["sort_key"] < fee["sort_key"]
    assert fee["sort_key"] < claims.target_for("app/report.py", 6, cites)["sort_key"]


def test_the_session_plants_in_the_strongest_claimed_module_and_records_the_claim(repo_and_pool, tmp_path):
    repo, pool = repo_and_pool
    ann = claims.annotate(pool, REC)
    best = min(claims.rank_key(c) for c in ann["candidates"])
    for seed in range(5):
        s = Session.start(repo, pool, seed=seed, python=PY, claims=REC)
        assert not s.refused
        assert claims.rank_key(dict(s.cand, claim_target=claims.target_for(s.cand["path"], s.cand["line"], claims.citations(REC)))) == best
        v = s.view()
        assert CLAIM not in json.dumps(v) and "c0" not in v["failing_output"]  # the Shield rule: no claim text in the view
        s.answer(s.cand["path"], s.cand["line"])
        rec = s.end(registry=tmp_path / "burned.json")
        t = rec["target"]
        assert t["cited"] and t["planted_module"] == t["cited_module"] == rec["candidate"]["module"]
        assert t["claim_text"] == claims.claim_text(REC, t["claim_id"]) == CLAIM and t["inside_cited_lines"]
        assert t["what_happened"]["found"] is True
    page = render_session(rec)
    assert "the résumé said" in page and "Claim targeted" in page and t["claim_text"] in page


def test_no_candidate_in_a_claimed_module_is_a_result_and_nothing_is_planted(repo_and_pool, tmp_path):
    repo, pool = repo_and_pool
    # the only cited module, report.py, has no validated bug
    elsewhere = record(("c1", "Python", "technology_use", "supported", "app/report.py", None, None),
                       ("c9", "Built the billing service", "ownership", "supported", "app/billing.py", 1, 40))
    before = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    s = Session.start(repo, pool, seed=0, python=PY, claims=elsewhere)
    assert s.refused == claims.NO_CLAIMED_MODULE == "no validated bug available in a module this résumé claims."
    assert s.wt is None and subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout == before
    rec = s.end(registry=tmp_path / "burned.json")
    assert rec["planted"] is False and rec["result"] == "no_claimed_module"
    assert rec["claim_targeting"]["candidates_in_claimed_modules"] == 0
    assert "a module this résumé claims" in render_session(rec)
    s2 = Session.start(repo, pool, seed=0, python=PY, claims=elsewhere, allow_uncited=True)  # only when asked
    rec2 = s2.end(registry=tmp_path / "burned.json")
    assert rec2["target"]["cited"] is False and rec2["target"]["message"] == claims.NO_CLAIMED_MODULE
    assert "Planted instead, by explicit choice" in render_session(rec2)


def test_the_shield_guard_refuses_a_claim_in_the_view_but_not_the_repos_own_words(repo_and_pool, tmp_path):
    repo, pool = repo_and_pool
    assert leaked_claims(f"output: {CLAIM}", [CLAIM], repo) == [CLAIM]
    own = "from app.ledger import balance"  # a sentence the repository itself contains
    assert leaked_claims(f"x {own} y", [own], repo) == []
    assert leaked_claims("Python everywhere", ["Python"], repo) == []  # too short to identify a claim
    s = Session.start(repo, pool, seed=0, python=PY, claims=REC)
    try:
        s.failing["output"] += CLAIM
        with pytest.raises(ShieldError):
            s.view()
    finally:
        s.end(registry=tmp_path / "burned.json")


def test_a_failed_start_never_leaves_a_worktree_and_a_relative_python_works(repo_and_pool, tmp_path, monkeypatch):
    repo, pool = repo_and_pool
    before = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    with pytest.raises(FileNotFoundError):
        Session.start(repo, pool, seed=0, python="no/such/python")
    assert subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout == before
    monkeypatch.chdir(Path(PY).parent)
    s = Session.start(repo, pool, seed=0, python="./" + Path(PY).name)  # relative to where the session was started
    assert "1 failed" in s.failing["output"]
    s.end(registry=tmp_path / "burned.json")


def test_generate_pool_validates_claimed_modules_first_and_marks_every_candidate(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW}))
    balance = record(("c0", CLAIM, "ownership", "supported", "app/ledger.py", 9, 15))
    for seed in range(3):  # whatever the seed, the cited span is validated first
        pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=1, claims=balance, seed=seed,
                             log=lambda *_: None)
        assert pool["status"] == "ok" and 9 <= pool["candidates"][0]["line"] <= 15
    assert pool["candidates"][0]["claim_target"]["verdict"] == "supported"
    assert pool["claim_targeting"]["candidates_in_claimed_modules"] == 1
    assert CLAIM not in json.dumps(pool["candidates"])  # the pool carries claim ids, never the text
