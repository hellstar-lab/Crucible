"""Stage 2: the artifact-implementation retrieval rule (r_artifact), MySQL in the technology lexicon, test-status rows
routed to inconclusive, and Stage 1 final amendment 3 (unjudged_by_guard never blocks retrieval or verification).
Deterministic except where FakeLLM / FakeJudge stand in for the model."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import make_git_fixtures as M
import test_target as T
from backend import llm, retrieval, verdicts
from backend.claims import modality, pipeline, routing
from backend.evidence import cache
from backend.retrieval import lexicon
from backend.retrieval.sources import load_view
from test_claims_pipeline import FakeLLM
from test_retrieval import identity
from test_verdicts import FakeJudge

POOL = b"""from concurrent.futures import ThreadPoolExecutor


class WorkerPool:
    def __init__(self, n):
        self._ex = ThreadPoolExecutor(max_workers=n)

    def submit(self, fn):
        return self._ex.submit(fn)
"""
SEM = b"""import threading


def make_gate(limit):
    return threading.BoundedSemaphore(limit)
"""
QUEUE = b"""import queue


def make_buffer(capacity):
    return queue.Queue(maxsize=capacity)
"""
SERVICE = b"""def lock_section(db, section_id):
    \"\"\"SELECT ... FOR UPDATE: other transactions wait here.\"\"\"
    return db.query(Section).filter_by(id=section_id).with_for_update().one()


def register(db, student, section):
    try:
        lock_section(db, section)
        db.commit()
    except Exception:
        db.rollback()
        raise
"""
MODELS = b"""from sqlalchemy import CheckConstraint, UniqueConstraint


class Registration:
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("student_id", "section_id"), CheckConstraint("seats >= 0"))
"""
TEST_SEM = b"""import threading


def test_gate():
    assert threading.Semaphore(1)
"""


def concurrency_service(repo):
    """Thread pool, semaphore, bounded queue, row lock, transactions, constraints; a semaphore inside a TEST (not
    implementation); no stored procedure anywhere."""
    M.init(repo)
    M.commit(repo, {"requirements.txt": b"sqlalchemy==2.0.30\npymysql==1.1.0\n", "src/worker_pool.py": POOL,
                    "src/db_semaphore.py": SEM, "src/bounded_queue.py": QUEUE}, M.CAND, "2024-01-01T10:00:00+00:00")
    M.commit(repo, {"src/registration_service.py": SERVICE, "src/models.py": MODELS, "tests/test_gate.py": TEST_SEM},
             M.CAND, "2024-02-01T10:00:00+00:00")


@pytest.fixture(scope="module")
def view(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("artifact")
    repo = tmp / "svc"
    concurrency_service(repo)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cache, "CACHE_DIR", tmp / "cache")
        report = T.run([f"{M.LOGIN}/svc"], {f"/repos/{M.LOGIN}/svc": T.meta(M.LOGIN, "svc")}, {f"{M.LOGIN}/svc": repo},
                       use_cache=True).targets[0]
    assert report.status == "resolved"
    return load_view(report, snapshot_root=tmp / "cache", checkout=repo, identity=identity())


def C(text, cid="c1"):
    rt = routing.route(text)
    return {"claim_id": cid, "text": text, "kind": rt["kind"], "checkable_from_code": rt["checkable_from_code"],
            "secondary_kinds": rt["secondary_kinds"], "why_not_checkable": rt["why_not_checkable"]}


# ---- MySQL ---------------------------------------------------------------------------------------------------------
def test_mysql_is_a_known_technology_found_through_its_driver(view):
    for text in ("Implementation Stack: MySQL", "MySQL database schema design"):
        assert lexicon.recognise(text) == ["mysql"]
    es = retrieval.retrieve(C("Implementation Stack: MySQL"), [view])
    assert es.retrieval_performed
    dep = [it for it in es.items if it.rule == "r_dependency"]
    assert dep and dep[0].path == "requirements.txt" and "pymysql" in dep[0].match_detail


# ---- r_artifact ----------------------------------------------------------------------------------------------------
def test_a_claim_naming_a_code_concept_is_routed_and_searched_in_the_code(view):
    c = C("Synchronization semaphores regulate execution threads")
    assert c["kind"] == "artifact_exists" and c["checkable_from_code"] == "yes"  # was "other": never searched
    es = retrieval.retrieve(c, [view])
    art = [it for it in es.items if it.rule == "r_artifact"]
    assert [it.path for it in art] == ["src/db_semaphore.py"]  # the semaphore in tests/ is not implementation
    # the citation is the implementing LINE (BoundedSemaphore), the function is named in the detail
    assert (art[0].start_line, art[0].end_line, art[0].kind) == (5, 5, "function") and "make_gate" in art[0].match_detail


def test_a_built_concept_is_an_ownership_claim_with_both_rules(view):
    c = C("Deployed worker thread pools")
    assert c["kind"] == "ownership"
    es = retrieval.retrieve(c, [view])
    rules = {it.rule for it in es.items}
    assert "r_artifact" in rules and "r_ownership" in rules  # the name (worker_pool.py) and the code (ThreadPoolExecutor)
    art = next(it for it in es.items if it.rule == "r_artifact")
    assert art.path == "src/worker_pool.py" and "ThreadPoolExecutor" in art.match_detail


@pytest.mark.parametrize("text,path,needle", [
    ("Row locks prevent conflicts during parallel bookings", "src/registration_service.py", "with_for_update"),
    ("Checks for duplicate entries", "src/models.py", "UniqueConstraint"),
    ("Fixed-capacity buffer queue", "src/bounded_queue.py", "Queue"),
])
def test_each_concept_is_cited_where_it_is_implemented(view, text, path, needle):
    es = retrieval.retrieve(C(text), [view])
    art = [it for it in es.items if it.rule == "r_artifact"]
    assert art and art[0].path == path and needle in art[0].match_detail


def test_an_absent_concept_is_looked_for_and_not_found(view):
    es = retrieval.retrieve(C("Designed registration stored procedures"), [view])
    assert es.retrieval_performed
    assert "r_artifact:stored_procedure:none" in es.searched.rules_fired
    assert not [it for it in es.items if it.rule == "r_artifact"]


def test_claims_naming_no_concept_route_as_before():
    assert routing.route("Passionate about clean code")["kind"] == "other"
    assert routing.route("Led a team of 4 engineers.")["kind"] == "role_or_team"
    assert routing.route("Built a caching layer with Redis")["kind"] == "technology_use"


# ---- Stage 1 final amendment 3: a claim the guard does not judge is never blocked (now: not_applicable) --------------
LABEL_VALUES = ["Implementation Stack: Python", "Implementation Stack: MySQL", "MySQL database schema design"]


def test_label_value_claims_the_guard_does_not_judge_reach_retrieval_and_verification(view, monkeypatch):
    monkeypatch.setattr(llm, "call", FakeLLM())
    claims = []
    for i, text in enumerate(LABEL_VALUES):
        out = pipeline.extract_from_text(text, headings=["Codebase Information"], decomposition_tier="nano")
        assert [(c.text, c.tense_guard) for c in out.claims] == [(text, "not_applicable")]  # was unjudged_by_guard
        claims.append(out.claims[0].model_copy(update={"claim_id": f"c{i}"}))
    sets = retrieval.retrieve_all(claims, [view])
    assert all(es.retrieval_performed and es.items for es in sets), [es.skip_reason for es in sets]
    monkeypatch.setattr(llm, "call", FakeJudge())
    run = verdicts.verdicts_for(claims, sets)
    assert run.not_judged == [] and len(run.verdicts) == 3
    assert not any("guard" in (v.matrix_row or "") for v in run.verdicts)


# ---- test-status rows ----------------------------------------------------------------------------------------------
def test_a_test_status_row_is_inconclusive_without_a_model_call(view, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test-status row must not reach the model")
    monkeypatch.setattr(llm, "call", boom)
    c = dict(C("The transaction test passes."), test_status="Pass")
    es = retrieval.retrieve(c, [view])
    v, n, _cost = verdicts.verdict_for(c, es)
    assert (v.verdict, v.matrix_row, n) == ("inconclusive", "gate:test_status", 0)
    assert "'Pass'" in v.reason and "a repository does not record" in v.reason


def test_test_status_comes_from_the_rows_status_cell():
    rows = [NS(text=f"Test Type: {t}; Status (Pass/Fail): {s}; Notes: n.", cells=[t, s, "n."], header=["Test Type", "Status (Pass/Fail)", "Notes"])
            for t, s in [("Transaction test", "Pass"), ("Locking test", "Failed"), ("High load test", "Pending")]]
    units = [NS(unit_id=f"u{i}", text=r.text) for i, r in enumerate(rows)]
    assert modality.test_status(units, [NS(row_lines=rows)]) == {"u0": "Pass", "u1": "Failed"}  # Pending: a non-assertion


# ---- claim track close-out: a guarantee over a mechanism that was found ---------------------------------------------
def test_a_guarantee_over_a_found_mechanism_is_inconclusive_citing_the_mechanism(view, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("one possible verdict: no model call")
    monkeypatch.setattr(llm, "call", boom)
    c = C("Row-level locking guarantees seating caps are never exceeded")
    es = retrieval.retrieve(c, [view])
    v, n, _cost = verdicts.verdict_for(c, es)
    assert (v.verdict, v.matrix_row, n) == ("inconclusive", "gate:guarantee_over_mechanism", 0)
    # the with_for_update() CALL (line 3), not the docstring that mentions FOR UPDATE (line 2): concurrent_reg's 107 vs 106
    assert (v.cited_item.path, v.cited_item.start_line) == ("src/registration_service.py", 3)
    assert "'guarantees'" in v.reason and "cannot establish" in v.reason


def test_a_guarantee_with_no_mechanism_found_takes_the_ordinary_path(view, monkeypatch):
    monkeypatch.setattr(llm, "call", FakeJudge([("no_evidence", None, 0.9, False)]))
    c = C("Stored procedures guarantee seats are never oversold")
    es = retrieval.retrieve(c, [view])
    v, _n, _cost = verdicts.verdict_for(c, es)
    assert v.matrix_row != "gate:guarantee_over_mechanism"


def test_a_claim_that_adds_a_guarantee_is_an_ungrounded_qualifier():
    from backend.claims import guards
    assert guards.ungrounded_qualifiers("The system always rejects duplicates", "The system rejects duplicates") == ["always"]
    src = "Row-level locking and ACID transactions guarantee that class seating caps are never exceeded."
    assert guards.ungrounded_qualifiers("Row-level locking guarantees caps are never exceeded", src) == []  # grounded
