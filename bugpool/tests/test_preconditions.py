"""Phase 5 on a real repository: the environment precondition, the stability gates, the docs telegraph check, the
visible file set, and the single-commit authorship refusal. Needs the spike venv (see test_bugpool.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("cosmic_ray")
pytest.importorskip("pytest_cov")

from bugpool import AUTHORSHIP_UNKNOWN, COULD_NOT_RUN, COVERAGE_INSUFFICIENT, INSUFFICIENT, docs_check, tools  # noqa: E402
from bugpool.baseline import precondition, run_suite, stability  # noqa: E402
from bugpool.candidates import enumerate_candidates  # noqa: E402
from bugpool.coverage_map import build  # noqa: E402
from bugpool.pool import generate_pool  # noqa: E402
from bugpool.session import Session, stranger_curve  # noqa: E402
from bugpool.validate import validate  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_bugpool import APP, FLOW, ME, PY, make_repo  # noqa: E402

QUIET = dict(log=lambda *_: None)
FLAKY = ("import os\nfrom pathlib import Path\n\n\ndef test_sometimes():\n    p = Path(os.environ['BUGPOOL_FLAKY'])\n"
         "    n = int(p.read_text()) if p.exists() else 0\n    p.write_text(str(n + 1))\n    assert n % 2 == 0\n")
DOC = ("# Ledger notes\n\nThe `fee` function charges 25 when the amount is at least 1000.\n\n"
       "- `balance` adds every entry after its fee.\n")


# ---- 1. the precondition: "could not run" vs "ran, coverage insufficient" ------------------------------------------
def test_an_import_error_is_could_not_run_with_the_error_not_insufficient_coverage(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": "import no_such_package_xyz\n" + FLOW}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, **QUIET)
    assert pool["status"] == "empty" and pool["refusal"] == COULD_NOT_RUN
    assert pool["message"] == COULD_NOT_RUN and pool["message"] != INSUFFICIENT
    assert "no_such_package_xyz" in pool["reason"]  # the underlying error, verbatim
    assert pool["precondition"]["status"] == "could_not_run"


def test_every_test_erroring_in_setup_is_could_not_run(tmp_path):
    conftest = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef db():\n    raise RuntimeError('database unreachable at 127.0.0.1:1')\n"
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "tests/conftest.py": conftest}))
    pre = precondition(run_suite(repo, PY))
    assert pre["status"] == "could_not_run" and "database unreachable" in pre["error"]


def test_a_missing_interpreter_is_could_not_run(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW}))
    pool = generate_pool(repo, [ME[1]], source="app", python=str(tmp_path / "no-python"), behaviour_modules=2, **QUIET)
    assert pool["refusal"] == COULD_NOT_RUN and "does not exist" in pool["reason"]


def test_a_suite_that_ran_without_a_behaviour_test_is_coverage_insufficient(tmp_path):
    unit = "from app.ledger import fee\n\n\ndef test_fee():\n    assert fee(5) == 0\n"
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_unit.py": unit}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, **QUIET)
    assert pool["precondition"]["status"] == "ran" and pool["precondition"]["passed"] == 1
    assert pool["refusal"] == COVERAGE_INSUFFICIENT and pool["message"] == INSUFFICIENT
    assert "no behaviour test" in pool["reason"]


def test_the_precondition_reports_skips_with_their_reasons(tmp_path):
    skip = "import pytest\n\n\n@pytest.mark.skip(reason='requires_mysql: no TEST_DATABASE_URL')\ndef test_db():\n    pass\n"
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "tests/test_db.py": skip}))
    pre = precondition(run_suite(repo, PY))
    assert pre["status"] == "ran" and pre["collected"] == 2
    assert pre["skipped"] == [{"test": "tests/test_db.py::test_db", "reason": "requires_mysql: no TEST_DATABASE_URL"}]


# ---- 2. stability: the baseline oracle and the 3-run kill ------------------------------------------------------------
def test_a_flaky_test_is_dropped_from_the_oracle_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGPOOL_FLAKY", str(tmp_path / "counter"))
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "tests/test_flaky.py": FLAKY}))
    stab = stability([run_suite(repo, PY) for _ in range(3)])
    assert stab["unstable"] == {"tests/test_flaky.py::test_sometimes": [["passed"], ["failed"], ["passed"]]}
    assert stab["oracle"] == ["tests/test_flow.py::test_month_end_flow"]
    (tmp_path / "counter").unlink()
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=3, **QUIET)
    assert "tests/test_flaky.py::test_sometimes" in pool["oracle"]["unstable"]
    assert pool["status"] == "ok"
    assert all("tests/test_flaky.py::test_sometimes" not in c["covering_tests"] for c in pool["candidates"])
    assert all(c["kill_runs"] == [[c["killing_test"]]] * 3 for c in pool["candidates"])


def test_a_kill_that_does_not_repeat_three_times_is_unstable(tmp_path, monkeypatch):
    once = ("import os\nfrom pathlib import Path\nfrom app.report import close\n\n\ndef test_month_end_flow():\n"
            "    p = Path(os.environ['BUGPOOL_FLAKY'])\n    n = int(p.read_text()) if p.exists() else 0\n"
            "    p.write_text(str(n + 1))\n    if n:\n        return  # checks only on its first run\n"
            "    assert close([50, 200, 1500, -30, 80]) == 1800\n    assert close([-5]) is None\n")
    counter = tmp_path / "counter"
    monkeypatch.setenv("BUGPOOL_FLAKY", str(counter))
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": once}))
    cmap = build(repo, "app", PY)
    cands, _ = enumerate_candidates(repo, ["app"])
    classes = []
    for c in cands:
        if not c.family:
            continue
        c.covering_tests = sorted(cmap.tests_for(c.path, c.start[0], c.end[0]))
        if not c.covering_tests:
            continue
        counter.unlink(missing_ok=True)
        v = validate(repo, c, PY, kill_runs=3)
        classes.append(v.cls)
        if v.cls == "unstable":
            assert v.kill_runs[0] == ["tests/test_flow.py::test_month_end_flow"] and v.kill_runs[1] == []
    assert "unstable" in classes and "clean" not in classes


# ---- 3. the docs telegraph check -------------------------------------------------------------------------------------
def test_docs_check_finds_prose_that_describes_the_mutated_line(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "docs/notes.md": DOC}))
    passages = docs_check.corpus(repo)
    swap = {"path": "app/ledger.py", "line": 2, "operator": "core/ReplaceComparisonOperator_GtE_Gt",
            "original_line": "    if amount >= 1000:"}
    d = docs_check.check(repo, swap, passages)
    assert d["described"] and d["hits_in_hidden_dirs"] == 1 and d["hits_visible_in_both"] == 0
    assert d["hits"][0]["source"] == "docs/notes.md:3" and d["hits"][0]["meaning"] == "at least"
    num = {"path": "app/ledger.py", "line": 3, "operator": "core/NumberReplacer", "original_line": "        return 25"}
    assert docs_check.check(repo, num, passages)["described"]  # "charges 25", in fee
    lt = {"path": "app/ledger.py", "line": 12, "operator": "core/ReplaceComparisonOperator_Lt_LtE",
          "original_line": "        if amount < 0:"}
    assert not docs_check.check(repo, lt, passages)["described"]  # "amount" is named, "less than" is not


def test_docs_check_reads_docstrings_and_counts_them_as_visible_in_both(tmp_path):
    files = dict(APP, **{"tests/test_flow.py": FLOW})
    files["app/report.py"] = files["app/report.py"].replace("def close(entries):\n",
                                                            'def close(entries):\n    """None when the balance is equal to zero."""\n')
    repo = make_repo(tmp_path, files)
    ne = {"path": "app/report.py", "line": 7, "operator": "core/ReplaceComparisonOperator_NotEq_Eq",
          "original_line": "    return b if b != 0 else None"}
    passages = docs_check.corpus(repo)
    # close's own docstring names close implicitly; NotEq means "not equal"/"differs", which it does not say
    assert not docs_check.check(repo, ne, passages)["described"]
    zero = dict(ne, operator="core/NumberReplacer")  # the literal 0: "zero" in the docstring of the enclosing function
    d = docs_check.check(repo, zero, passages)
    assert d["described"] and d["hits_visible_in_both"] == 1 and d["hits"][0]["kind"] == "docstring"


def test_docs_telegraph_is_recorded_on_pooled_candidates_never_rejected(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "docs/notes.md": DOC}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=10, max_per_killing_test=10,
                         max_per_module=10, **QUIET)
    assert pool["status"] == "ok"
    checks = [c["checks"]["docs_telegraph"] for c in pool["candidates"]]
    assert all("described" in d for d in checks)
    assert pool["stats"]["docs_telegraph"]["of"] == len(checks)
    assert pool["stats"]["docs_telegraph"]["rule"] == "recorded, never used to reject"


# ---- 4. the visible file set: one implementation for the participant and the stranger --------------------------------
def test_condition_code_hides_docs_everywhere_and_docs_shows_it(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "docs/notes.md": DOC}))
    code, docs = tools.hidden_for("code"), tools.hidden_for("docs")
    assert "docs/notes.md" not in tools.tree(repo, code) and "docs/notes.md" in tools.tree(repo, docs)
    assert tools.file_open(repo, "docs/notes.md", hide=code)[1] is None
    assert tools.file_open(repo, "app/../docs/notes.md", hide=code)[1] is None  # no path trick
    assert tools.file_open(repo, "docs/notes.md", hide=docs)[1] == "docs/notes.md"
    assert "docs/" not in tools.search(repo, "charges", hide=code)
    assert tools.search(repo, "charges", "docs", hide=code).startswith("error")
    assert "docs/notes.md:3" in tools.search(repo, "charges", hide=docs)
    with pytest.raises(ValueError):
        tools.hidden_for("everything")


def test_a_session_under_condition_code_never_shows_docs(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW, "docs/notes.md": DOC}))
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=1, **QUIET)
    s = Session.start(repo, pool, seed=0, python=PY, condition="code")
    try:
        assert "docs/notes.md" not in s.view()["files"]
        assert s.file_open("docs/notes.md").startswith("error")
        assert "docs/" not in s.search("charges")
    finally:
        rec = s.end(registry=tmp_path / "burned.json")
    assert rec["condition"] == "code" and rec["hidden"] == ["docs"]
    s2 = Session.start(repo, pool, seed=0, python=PY, condition="docs")
    try:
        assert "docs/notes.md" in s2.view()["files"]
    finally:
        s2.end(registry=tmp_path / "burned.json")


def test_the_stranger_curve_is_used_only_under_the_same_condition():
    data = {"budget": 40, "condition": "code", "results": [{"candidate_id": "x", "found": True, "actions_to_correct": 5,
                                                            "localisation_cost": 2}]}
    assert stranger_curve(data, "x", "code")["localisation_cost"] == 2
    assert stranger_curve(data, "x", "docs") is None


# ---- 5. authorship: one commit cannot tell teammates apart --------------------------------------------------------
def test_a_single_commit_repo_is_refused_unless_overridden(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW}), commits=1)
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, **QUIET)
    assert pool["refusal"] == AUTHORSHIP_UNKNOWN and pool["authorship"]["commits"] == 1
    assert "precondition" not in pool  # refused before anything ran
    ok = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=1,
                       single_commit_override="measurement session, not a hiring signal", **QUIET)
    assert ok["status"] == "ok"
    assert ok["authorship"]["override"]["reason"] == "measurement session, not a hiring signal"


def test_a_burned_registry_outside_the_repo_leaves_the_repo_untouched(tmp_path):
    repo = make_repo(tmp_path, dict(APP, **{"tests/test_flow.py": FLOW}))
    reg = tmp_path / "elsewhere" / "burned.json"
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=1, burned_registry=reg, **QUIET)
    s = Session.start(repo, pool, seed=0, python=PY)
    s.end(registry=reg)
    assert reg.exists() and not (repo / ".bugpool").exists()
    s2 = Session.start(repo, pool, seed=0, python=PY)
    assert s2.end(burn=False)["burned"] is False  # the stranger's path: no registry is written anywhere
    assert not (repo / ".bugpool").exists()
