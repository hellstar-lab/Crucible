"""bugpool: the offline pool generator and the session's refusal path. Needs cosmic-ray + pytest-cov (the spike venv):

    <spike-venv>/bin/python -m pytest bugpool/tests -q
Not collected by the main suite (pytest.ini testpaths = tests)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cosmic_ray")
pytest.importorskip("pytest_cov")

from bugpool import INSUFFICIENT  # noqa: E402
from bugpool.candidates import family  # noqa: E402
from bugpool.coverage_map import build, test_key as key_of  # noqa: E402
from bugpool.pool import generate_pool  # noqa: E402
from bugpool.session import plant  # noqa: E402

ME = ("Alex Sample", "alex@example.com")
PY = sys.executable


def _git(repo, *args, env=None):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def make_repo(tmp: Path, files: dict[str, str]) -> Path:
    import os
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for p, text in files.items():
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": ME[0], "GIT_AUTHOR_EMAIL": ME[1], "GIT_COMMITTER_NAME": ME[0],
           "GIT_COMMITTER_EMAIL": ME[1], "GIT_AUTHOR_DATE": "2024-01-01T10:00:00", "GIT_COMMITTER_DATE": "2024-01-01T10:00:00"}
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init", env=env)
    return repo


APP = {
    "pytest.ini": "[pytest]\npythonpath = .\n",
    "app/__init__.py": "",
    "app/ledger.py": ("def fee(amount):\n    if amount >= 1000:\n        return 25\n    if amount > 100:\n        return 5\n"
                      "    return 0\n\n\ndef balance(entries):\n    total = 0\n    for amount in entries:\n        if amount < 0:\n"
                      "            continue\n        total += amount - fee(amount)\n    return total\n"),
    "app/report.py": ("from app.ledger import balance\n\n\ndef close(entries):\n    b = balance(entries)\n"
                      "    return b if b != 0 else None\n"),
}
FLOW = ("from app.report import close\n\n\ndef test_month_end_flow():\n"
        "    assert close([50, 200, 1500, -30, 80]) == 1800\n    assert close([-5]) is None\n")


def test_whitelist():
    assert family("core/ReplaceComparisonOperator_Lt_LtE") == "comparison swap"
    assert family("core/ReplaceComparisonOperator_Is_IsNot") == "is / is not"
    assert family("core/ReplaceComparisonOperator_Is_Gt") is None  # only raises TypeError
    assert family("core/ReplaceBinaryOperator_Add_BitXor") is None
    assert family("core/ReplaceBreakWithContinue") == "break <-> continue"


def test_test_keys_collapse_parametrized_ids():
    assert key_of("tests/t.py::test_a[a26156bf]|run") == "tests/t.py::test_a"
    assert key_of("tests/t.py::TestX::test_b[1-2]|setup") == "tests/t.py::TestX::test_b"
    assert key_of("") is None
    # measured on Crucible: a parameter id containing "|" was cut at the first "|" and kept a dangling "["
    assert key_of("tests/test_claims_skills.py::test_split_items[Python; Go | Rust \u2022 C++-expected2]|run") == \
        "tests/test_claims_skills.py::test_split_items"


def test_line_map_is_stable_across_three_runs_despite_a_random_parameter(tmp_path):
    files = dict(APP, **{"tests/test_flow.py": (
        "import secrets\nimport pytest\nfrom app.report import close\n\n\n"
        "@pytest.mark.parametrize('salt', [secrets.token_hex(4)])\n"
        "def test_flow(salt):\n    assert close([50, 200]) == 245 and salt\n")})
    repo = make_repo(tmp_path, files)
    maps = [build(repo, "app", PY).as_json() for _ in range(3)]
    assert maps[0] and maps[0] == maps[1] == maps[2]
    assert all("[" not in t for m in maps[0].values() for ts in m.values() for t in ts)


def test_refusal_on_a_repo_with_no_tests(tmp_path):
    repo = make_repo(tmp_path, {k: v for k, v in APP.items() if not k.startswith("tests/")})
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=1, log=lambda *_: None)
    assert pool["status"] == "empty" and pool["candidates"] == [] and pool["message"] == INSUFFICIENT
    assert pool["reason"] == "the repository has no tests"
    assert "not built" in pool["second_tier"]
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    res = plant(pool, repo)
    assert res == {"planted": False, "message": INSUFFICIENT, "reason": "the repository has no tests", "candidate": None}
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == before
    assert subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.count("\n") == 1


def test_refusal_when_no_test_is_a_behaviour_test(tmp_path):
    files = dict(APP, **{"tests/test_unit.py": "from app.ledger import fee\n\n\ndef test_x():\n    assert fee(1) == 0\n"})
    repo = make_repo(tmp_path, files)
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, log=lambda *_: None)
    assert pool["status"] == "empty" and pool["reason"].startswith("no behaviour test")


def test_a_repo_with_a_behaviour_test_yields_a_plantable_pool(tmp_path):
    files = dict(APP, **{"tests/test_flow.py": FLOW})
    repo = make_repo(tmp_path, files)
    pool = generate_pool(repo, [ME[1]], source="app", python=PY, behaviour_modules=2, target=5, log=lambda *_: None)
    assert pool["status"] == "ok" and pool["candidates"], pool
    for c in pool["candidates"]:
        assert {"path", "line", "diff", "killing_test", "covering_tests", "operator", "checks"} <= set(c)
        assert c["killing_test"] == "tests/test_flow.py::test_month_end_flow" and c["checks"]["clean_kill"]
        assert not c["checks"]["exception_at_site"] and c["checks"]["telegraph_hits"] == []
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0  # the real tree was never mutated
    res = plant(pool, repo, seed=1)
    assert res["planted"] and subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 1
    r = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", res["candidate"]["killing_test"]], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 1  # the planted bug turns its killing test red
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)
    json.dumps(pool)  # the pool is plain JSON
