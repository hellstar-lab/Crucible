"""Phase 3.6: target-repository mode. Real git repositories on disk, a mocked
GitHub API (zero real calls), local clones. No model calls."""

import json
import subprocess

import httpx
import pytest

import make_git_fixtures as M
from backend.evidence import cache
from backend.evidence.dependencies import parse_manifests
from backend.evidence.functions import extract_functions
from backend.evidence.github_client import GitHubClient
from backend.evidence.readme import extract_claims
from backend.evidence.resolve import resolve_target, settle_ref
from backend.evidence.target import Options, analyze_targets

LOGIN = M.LOGIN
DAY = "2024-{:02d}-{:02d}T10:00:00+00:00"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def meta(owner, repo, **kw):
    base = {"full_name": f"{owner}/{repo}", "name": repo, "owner": {"login": owner}, "default_branch": "main",
            "size": 10, "fork": False, "archived": False, "disabled": False, "visibility": "public",
            "clone_url": f"https://github.com/{owner}/{repo}.git", "created_at": "2024-01-01T00:00:00Z",
            "pushed_at": "2024-06-01T00:00:00Z"}
    base.update(kw)
    return base


PROFILE = {"login": LOGIN, "id": M.USER_ID, "email": M.CAND[1], "name": "Alex Sample"}


def client_for(routes: dict, calls: list | None = None, remaining: str = "4000") -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(path)
        hit = routes.get(path)
        if callable(hit):
            hit = hit(request)
        if hit is None:
            return httpx.Response(404, json={"message": "Not Found"}, headers={"x-ratelimit-remaining": remaining})
        status, body, headers = hit if isinstance(hit, tuple) else (200, hit, {})
        headers = {"x-ratelimit-remaining": remaining, **headers}
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)
    return GitHubClient(token="", transport=httpx.MockTransport(handler))


def run(raws, routes, local: dict, user=LOGIN, emails=None, calls=None, **opt):
    opts = Options(allow_local=True, clone_url_for={k: str(v) for k, v in local.items()},
                   use_cache=opt.pop("use_cache", False), **opt)
    routes = {f"/users/{LOGIN}": PROFILE, **routes}
    return analyze_targets(raws, user=user, emails=emails, opts=opts, client=client_for(routes, calls))


def shas(repo):
    return subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True, text=True).stdout.split()


# --------------------------------------------------------------------------
# Step 1: resolver matrix (pure)
# --------------------------------------------------------------------------

RESOLVER_CASES = [
    ("owner/repo", "repo", "owner", "repo", None, None, False),
    ("owner/repo/", "repo", "owner", "repo", None, None, False),
    ("owner/repo.git", "repo", "owner", "repo", None, None, False),
    ("https://github.com/owner/repo", "repo", "owner", "repo", None, None, False),
    ("http://github.com/owner/repo", "repo", "owner", "repo", None, None, False),
    ("https://www.github.com/owner/repo", "repo", "owner", "repo", None, None, False),
    ("https://github.com/owner/repo.git", "repo", "owner", "repo", None, None, False),
    ("git@github.com:owner/repo.git", "repo", "owner", "repo", None, None, False),
    ("https://github.com/owner/repo/tree/main", "repo", "owner", "repo", "main", None, False),
    ("https://github.com/owner/repo/tree/main/packages/api", "repo", "owner", "repo", "main", "packages/api", True),
    ("https://github.com/owner/repo/blob/main/src/x.py", "repo", "owner", "repo", "main", "src/x.py", True),
    ("https://github.com/owner/repo/pull/42", "repo", "owner", "repo", None, None, False),
    ("https://github.com/owner/repo#readme", "repo", "owner", "repo", None, None, False),
    ("https://github.com/owner/repo?tab=readme-ov-file", "repo", "owner", "repo", None, None, False),
    ("username", "account", "username", None, None, None, False),
    ("OWNER/REPO", "repo", "OWNER", "REPO", None, None, False),
]


@pytest.mark.parametrize("raw,kind,owner,repo,ref,path,ambiguous", RESOLVER_CASES)
def test_resolver_matrix(raw, kind, owner, repo, ref, path, ambiguous):
    t = resolve_target(raw)
    assert (t.kind, t.owner, t.repo, t.ref, t.scoped_path, t.ref_ambiguous) == (kind, owner, repo, ref, path, ambiguous)


@pytest.mark.parametrize("raw,warning", [
    ("http://github.com/owner/repo", "upgraded to https"), ("https://github.com/owner/repo/pull/42", "ignored"),
    ("https://github.com/owner/repo#readme", "fragment ignored"), ("https://github.com/owner/repo?tab=x", "query"),
])
def test_resolver_warnings(raw, warning):
    assert any(warning in w for w in resolve_target(raw).parse_warnings)


def test_resolver_case_preserved_but_compared_insensitively():
    assert resolve_target("OWNER/REPO").key == resolve_target("https://github.com/owner/repo").key
    assert resolve_target("OWNER/REPO").full_name == "OWNER/REPO"


@pytest.mark.parametrize("raw", ["", "  ", "https://gitlab.com/o/r", "o/r;rm", "o/r rm -rf /", "../etc/passwd",
                                 "o/..", "https://github.com/o/r/tree/main/../x", "single.git",
                                 "ftp://github.com/o/r", "https://user:pw@github.com/o/r", "-oProxy/x"])
def test_resolver_rejects_without_raising(raw):
    t = resolve_target(raw)
    assert t.kind == "invalid" and t.invalid_reason


def test_ambiguous_ref_candidates_and_settling():
    t = resolve_target("https://github.com/o/r/tree/feature/new-thing/src")
    assert t.ref_ambiguous
    assert t.ref_candidates == [("feature", "new-thing/src"), ("feature/new-thing", "src"),
                                ("feature/new-thing/src", None)]
    settled = settle_ref(t, ["main", "feature/new-thing"], "main")
    assert (settled.ref, settled.scoped_path, settled.ref_ambiguous) == ("feature/new-thing", "src", False)
    both = settle_ref(t, ["feature", "feature/new-thing"], "main")  # two matches: do not guess
    assert (both.ref, both.scoped_path) == ("main", None) and any("fell back" in w for w in both.parse_warnings)
    none = settle_ref(t, ["main"], "main")
    assert (none.ref, none.scoped_path) == ("main", None)


# --------------------------------------------------------------------------
# Step 10 fixtures (real repos) through the full pipeline
# --------------------------------------------------------------------------


def test_a_single_commit_import_has_no_confidence(tmp_path):
    repo = tmp_path / "single"
    M.init(repo)
    M.commit(repo, {"src/app.py": M.code("app", 30), "src/util.py": M.code("util", 20)}, M.CAND, DAY.format(1, 1))
    report = run(["o/single"], {"/repos/o/single": meta("o", "single")}, {"o/single": repo})
    [r] = report.targets
    assert r.status == "resolved" and r.mode == "blame"
    a = r.authorship
    assert a.authorship_confidence == "none" and a.authored_ratio_meaningful is False
    assert a.authored_ratio == 1.0  # still reported, but flagged
    assert "one commit" in a.authorship_confidence_reason
    assert r.repo_facts.history.committed_in_one_session is True
    assert all(x.kind == "unverified_authorship" for x in r.question_surface.anchors)


def test_b_multi_author_over_30_days_is_high_confidence(tmp_path):
    repo = tmp_path / "team"
    M.init(repo)
    for i, (who, month, day) in enumerate([(M.CAND, 1, 1), (M.BOB, 1, 9), (M.CAROL, 1, 20), (M.CAND, 2, 2),
                                           (M.BOB, 2, 15), (M.CAND, 3, 1)]):
        M.commit(repo, {f"src/m{i}.py": M.code(f"m{i}", 10)}, who, DAY.format(month, day))
    report = run(["o/team"], {"/repos/o/team": meta("o", "team")}, {"o/team": repo})
    a = report.targets[0].authorship
    assert a.authorship_confidence == "high" and a.authored_ratio_meaningful is True
    assert a.authored_ratio == 0.5  # 3 of 6 equal files


def test_c_email_differs_from_login_shows_unmatched_author_and_gap(tmp_path):
    repo = tmp_path / "unlinked"
    M.init(repo)
    for i, d in enumerate((1, 5, 12, 20, 28)):
        M.commit(repo, {f"src/f{i}.py": M.code(f"f{i}", 12)}, M.UNLINKED, DAY.format(1, d))
    profile = {**PROFILE, "email": None}
    report = run([f"{LOGIN}/unlinked"], {f"/users/{LOGIN}": profile, f"/repos/{LOGIN}/unlinked": meta(LOGIN, "unlinked")},
                 {f"{LOGIN}/unlinked": repo})
    a = report.targets[0].authorship
    assert a.authored_ratio == 0.0
    assert a.unmatched_authors and a.unmatched_authors[0].email_redacted == "ale...@<local-machine>"
    assert a.likely_identity_gap is True


def test_d_scoped_subdirectory_excludes_files_outside(tmp_path):
    repo = tmp_path / "mono"
    M.init(repo)
    M.commit(repo, {"packages/api/a.py": M.code("a", 20)}, M.CAND, DAY.format(1, 1))
    M.commit(repo, {"packages/web/b.py": M.code("b", 50)}, M.BOB, DAY.format(2, 1))
    routes = {"/repos/o/mono": meta("o", "mono"), "/repos/o/mono/branches": [{"name": "main"}]}
    report = run(["https://github.com/o/mono/tree/main/packages/api"], routes, {"o/mono": repo})
    [r] = report.targets
    assert r.repo_facts.scoped_path == "packages/api" and r.repo_facts.ref_analyzed == "main"
    assert r.repo_facts.total_source_lines == 20 and r.repo_facts.source_files == 1
    assert r.authorship.authored_ratio == 1.0  # Bob's packages/web is outside the scope
    assert all(not h.path.startswith("packages/web") for h in r.authorship.hot_files)


def test_e_vendored_directory_still_excluded(tmp_path):
    repo = tmp_path / "vend"
    M.init(repo)
    M.commit(repo, {"src/main.js": M.code("main", 30)}, M.CAND, DAY.format(1, 1))
    M.commit(repo, {"node_modules/lib/index.js": M.code("lib", 300), "vendor/x.py": M.code("x", 100)}, M.BOB,
             DAY.format(1, 10))
    report = run(["o/vend"], {"/repos/o/vend": meta("o", "vend")}, {"o/vend": repo})
    r = report.targets[0]
    assert r.repo_facts.excluded_files_by_reason["vendored_path"] == 2
    assert r.repo_facts.total_source_lines == 30 and r.authorship.authored_ratio == 1.0
    anchored = {x.path for x in r.question_surface.anchors}
    assert not any(p.startswith(("node_modules/", "vendor/")) for p in anchored)


# --------------------------------------------------------------------------
# Step 2/3: precheck statuses, 301, forks (mocked API)
# --------------------------------------------------------------------------


def test_statuses_never_kill_the_batch(tmp_path):
    repo = tmp_path / "ok"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    routes = {"/repos/o/ok": meta("o", "ok"), "/repos/o/denied": (403, {"message": "SAML enforced"}, {}),
              "/repos/o/archived": meta("o", "archived", archived=True)}
    report = run(["o/missing", "o/denied", "not a repo", "o/ok", "o/archived", "https://github.com/O/OK"], routes,
                 {"o/ok": repo, "o/archived": repo})
    by = {r.raw_input: r for r in report.targets}
    assert by["o/missing"].status == "not_found" and by["o/missing"].http_status == 404
    assert by["o/denied"].status == "access_denied" and by["o/denied"].http_status == 403
    assert by["not a repo"].status == "invalid_input"
    assert by["o/ok"].status == "resolved"
    assert by["o/archived"].status == "archived_ok" and by["o/archived"].repo_facts.archived is True
    assert by["https://github.com/O/OK"].status == "duplicate"
    for r in report.targets:
        if r.status not in ("resolved", "archived_ok"):
            assert r.repo_facts is None and r.authorship is None  # not measured: nothing numeric


def test_redirect_301_records_both_names(tmp_path):
    repo = tmp_path / "renamed"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    routes = {"/repos/old/name": (301, {"message": "Moved Permanently", "url": "https://api.github.com/repositories/42"},
                                  {"location": "https://api.github.com/repositories/42"}),
              "/repositories/42": meta("newowner", "name")}
    report = run(["old/name"], routes, {"newowner/name": repo})
    f = report.targets[0].repo_facts
    assert f.full_name == "newowner/name" and f.redirected_from == "old/name"


def _fork_repo(tmp_path):
    repo = tmp_path / "fork"
    M.init(repo)
    M.commit(repo, {"lib/upstream.py": M.code("up", 80)}, M.BOB, DAY.format(1, 1))
    M.commit(repo, {"lib/more.py": M.code("more", 20)}, M.BOB, DAY.format(1, 20))
    M.commit(repo, {"src/mine.py": M.code("mine", 25)}, M.CAND, DAY.format(2, 10))
    M.commit(repo, {"src/also.py": M.code("also", 15)}, M.CAND, DAY.format(3, 1))
    return repo


def test_fork_reports_whole_repo_and_fork_delta(tmp_path):
    repo = _fork_repo(tmp_path)
    ahead = shas(repo)[:2]  # the two candidate commits on top of upstream
    fork_meta = meta(LOGIN, "fork", fork=True, parent={"full_name": "up/orig", "default_branch": "main"},
                     source={"full_name": "up/orig"})
    routes = {f"/repos/{LOGIN}/fork": fork_meta,
              f"/repos/up/orig/compare/main...{LOGIN}:main": {"ahead_by": 2, "behind_by": 0, "total_commits": 2,
                                                              "commits": [{"sha": s} for s in ahead],
                                                              "files": [{"filename": "src/mine.py"}]}}
    report = run([f"{LOGIN}/fork"], routes, {f"{LOGIN}/fork": repo})
    r = report.targets[0]
    assert (r.repo_facts.parent_full_name, r.repo_facts.fork_ahead_by, r.repo_facts.files_truncated) == ("up/orig", 2, False)
    a = r.authorship
    assert a.authored_ratio_whole_repo == pytest.approx(40 / 140, abs=1e-6)  # misleading: includes upstream lines
    assert a.authored_ratio_fork_delta == 1.0 and a.authored_ratio == 1.0 and a.fork_delta_source_lines == 40


def test_fork_compare_failure_sets_both_none(tmp_path):
    repo = _fork_repo(tmp_path)
    fork_meta = meta(LOGIN, "fork", fork=True, parent={"full_name": "up/orig", "default_branch": "main"})
    routes = {f"/repos/{LOGIN}/fork": fork_meta,
              f"/repos/up/orig/compare/main...{LOGIN}:main": (422, {"message": "not in the same network"}, {})}
    a = run([f"{LOGIN}/fork"], routes, {f"{LOGIN}/fork": repo}).targets[0].authorship
    assert a.authored_ratio_whole_repo is None and a.authored_ratio_fork_delta is None and a.authored_ratio is None
    assert "compare" in a.fork_delta_reason


def test_fork_with_incomplete_compare_is_not_pretended_complete(tmp_path):
    repo = _fork_repo(tmp_path)
    fork_meta = meta(LOGIN, "fork", fork=True, parent={"full_name": "up/orig", "default_branch": "main"})
    routes = {f"/repos/{LOGIN}/fork": fork_meta,
              f"/repos/up/orig/compare/main...{LOGIN}:main": {"ahead_by": 900, "behind_by": 3, "total_commits": 900,
                                                              "commits": [{"sha": "a" * 40}],
                                                              "files": [{"filename": f"f{i}"} for i in range(300)]}}
    r = run([f"{LOGIN}/fork"], routes, {f"{LOGIN}/fork": repo}).targets[0]
    assert r.repo_facts.files_truncated is True
    assert r.authorship.authored_ratio_fork_delta is None and "of 900 commits" in r.authorship.fork_delta_reason


def test_rate_limit_floor_stops_and_reports(tmp_path):
    routes = {"/repos/o/a": meta("o", "a"), "/repos/o/b": meta("o", "b")}
    opts = Options(allow_local=True, use_cache=False)
    client = client_for({f"/users/{LOGIN}": PROFILE, **routes}, remaining="5")
    report = analyze_targets(["o/a", "o/b"], user=LOGIN, opts=opts, client=client)
    assert report.stopped_reason and "X-RateLimit-Remaining=5" in report.stopped_reason
    assert any(r.status == "rate_limited" for r in report.targets)


def test_too_large_goes_to_degraded_api_mode():
    big = meta("o", "big", size=400 * 1024)  # 400 MiB packed > 300
    commits = [{"sha": f"{i:040x}", "commit": {"author": {"date": f"2024-0{i}-01T00:00:00Z"},
                                               "message": f"change {i}\n\nbody"}} for i in range(1, 4)]
    routes = {"/repos/o/big": big, "/repos/o/big/commits": commits,
              **{f"/repos/o/big/commits/{c['sha']}": {"files": [{"filename": f"src/f{i}.py"}]}
                 for i, c in enumerate(commits)},
              "/repos/o/big/git/trees/main": {"tree": [{"path": "README.md", "type": "blob"},
                                                       {"path": "package.json", "type": "blob"},
                                                       {"path": "src/a.js", "type": "blob"}], "truncated": False},
              "/repos/o/big/contents/README.md": "This service schedules deliveries with Redis queues.\n",
              "/repos/o/big/contents/package.json": json.dumps({"dependencies": {"redis": "^4"}})}
    report = run(["o/big"], routes, {})
    r = report.targets[0]
    assert r.status == "too_large" and r.mode == "api_commits_only"
    a = r.authorship
    assert a.mode == "api_commits_only" and a.authored_ratio is None and a.authored_source_lines is None
    assert a.commit_count == 3 and a.files_touched == ["src/f0.py", "src/f1.py", "src/f2.py"]
    assert a.commit_message_heads[0] == "change 3"
    assert r.repo_facts.dependencies[0].name == "redis" and r.repo_facts.readme_claims


def test_repo_only_without_identity_gives_facts_not_authorship(tmp_path):
    repo = tmp_path / "anon"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    report = run(["o/anon"], {"/repos/o/anon": meta("o", "anon")}, {"o/anon": repo}, user=None)
    r = report.targets[0]
    assert r.repo_facts and r.authorship is None and "no identity" in r.identity_unresolved_reason
    assert r.question_surface.anchors == []  # nothing attributable -> no anchors


def test_user_plus_repo_does_not_enumerate_account(tmp_path):
    repo = tmp_path / "x"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    calls: list[str] = []
    report = run(["o/x"], {"/repos/o/x": meta("o", "x")}, {"o/x": repo}, calls=calls)
    assert f"/users/{LOGIN}/repos" not in calls
    assert "NOT enumerated" in report.input_spec.run_mode


def test_extra_email_gives_identity_without_user(tmp_path):
    repo = tmp_path / "e"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    report = run(["o/e"], {"/repos/o/e": meta("o", "e")}, {"o/e": repo}, user=None, emails=[M.CAND[1]])
    a = report.targets[0].authorship
    assert a.authored_ratio == 1.0 and report.input_spec.extra_emails_count == 1


# --------------------------------------------------------------------------
# Step 9: cache
# --------------------------------------------------------------------------


def test_cache_hit_and_ruleset_invalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    repo = tmp_path / "c"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    routes = {"/repos/o/c": meta("o", "c")}
    assert run(["o/c"], routes, {"o/c": repo}, use_cache=True).targets[0].cache_hit is False
    second = run(["o/c"], routes, {"o/c": repo}, use_cache=True).targets[0]
    assert second.cache_hit is True and second.authorship.authored_ratio == 1.0
    monkeypatch.setattr(cache, "ruleset_version", lambda: "changed-ruleset")
    assert run(["o/c"], routes, {"o/c": repo}, use_cache=True).targets[0].cache_hit is False


def test_cached_snapshot_holds_no_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    repo = tmp_path / "c2"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, DAY.format(1, 1))
    run(["o/c2"], {"/repos/o/c2": meta("o", "c2")}, {"o/c2": repo}, use_cache=True)
    [entry] = list((tmp_path / "cache").glob("*.json"))
    assert "profile_email" not in entry.read_text() and LOGIN not in entry.read_text()


# --------------------------------------------------------------------------
# Step 7: question surface building blocks
# --------------------------------------------------------------------------


def test_lizard_and_tree_sitter_agree_on_python_and_js(tmp_path):
    py = b"import functools\n\n\n@functools.lru_cache\ndef f(n):\n    if n < 2:\n        return n\n    return f(n - 1)\n"
    js = b"function g(a) {\n  if (a) { return 1; }\n  return 0;\n}\nconst h = (x) => {\n  return x * 2;\n};\n"
    for path, src in (("a.py", py), ("a.js", js)):
        fns = extract_functions(path, src)
        assert fns and all(f.boundaries_agree for f in fns), [(f.name, f.start_line, f.lizard_start_line) for f in fns]


def test_boundary_disagreement_is_flagged_not_trusted():
    c = b"static int\nadd(int a, int b)\n{\n    return a + b;\n}\n"
    [fn] = extract_functions("a.c", c)
    assert (fn.start_line, fn.end_line) == (1, 5)  # tree-sitter: whole node, return type included
    assert (fn.lizard_start_line, fn.lizard_end_line) == (2, 5)
    assert fn.boundaries_agree is False


def test_bug_targets_and_hard_anchor_rule(tmp_path):
    complex_fn = (b"def route(kind, n):\n    total = 0\n    for i in range(n):\n        if kind == 'a':\n"
                  b"            total += i\n        elif kind == 'b':\n            total -= i\n        else:\n"
                  b"            total *= 2\n    return total\n")
    repo = tmp_path / "bt"
    M.init(repo)
    for i, d in enumerate((1, 8, 15, 22, 29)):
        files = {"src/route.py": complex_fn} if i == 0 else {f"src/n{i}.py": M.code(f"n{i}", 5)}
        M.commit(repo, files, M.CAND, DAY.format(1, d))
    M.commit(repo, {"third_party/lib.py": complex_fn.replace(b"route", b"other")}, M.BOB, DAY.format(2, 5))
    r = run(["o/bt"], {"/repos/o/bt": meta("o", "bt")}, {"o/bt": repo}).targets[0]
    q = r.question_surface
    eligible = [b for b in q.bug_targets if b.eligible]
    assert [b.name for b in eligible] == ["route"] and eligible[0].ccn >= 3
    assert all(not a.path.startswith("third_party/") for a in q.anchors)  # vendored code is never an anchor
    assert any(a.kind == "authored_function" and a.path == "src/route.py" for a in q.anchors)


def test_claim_vs_code_mechanical_verdicts():
    from backend.evidence.question_surface import check_claims

    snap = {"readme_claims": [{"text": "Built with React and Redis for caching.", "kind": "sentence", "line": 1},
                              {"text": "A small tool that keeps your notes tidy.", "kind": "sentence", "line": 2},
                              {"text": "The API is served with FastAPI behind Docker.", "kind": "sentence", "line": 3}],
            "dependencies": [{"manifest_path": "package.json", "name": "react"},
                             {"manifest_path": "requirements.txt", "name": "fastapi"}],
            "imports": {}, "files_in_scope": ["Dockerfile", "package.json"]}
    out = {c.claim[:12]: c for c in check_claims(snap)}
    assert out["Built with R"].verdict == "unsupported" and "Redis" in out["Built with R"].evidence_against[0]
    assert out["A small tool"].verdict == "unclear"
    assert out["The API is s"].verdict == "supported"


# --------------------------------------------------------------------------
# dependency manifests and README extraction
# --------------------------------------------------------------------------


def test_every_manifest_ecosystem_parses():
    files = {
        "package.json": json.dumps({"dependencies": {"react": "^18"}, "devDependencies": {"vite": "5"}}),
        "requirements.txt": "fastapi==0.110\n# comment\nuvicorn[standard]>=0.29 ; python_version>'3'\n-r other.txt\n",
        "pyproject.toml": '[project]\ndependencies = ["httpx>=0.27"]\n[tool.poetry.dependencies]\npython = "^3.11"\nrich = "13"\n',
        "pom.xml": "<project><dependencies><dependency><groupId>org.x</groupId><artifactId>core</artifactId>"
                   "<version>1.2</version></dependency></dependencies></project>",
        "build.gradle": "dependencies {\n implementation 'com.google.guava:guava:33.0'\n}\n",
        "go.mod": "module m\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n\tgolang.org/x/sys v0.1.0 // indirect\n)\n",
        "Cargo.toml": '[dependencies]\nserde = { version = "1", features = ["derive"] }\ntokio = "1"\n',
        "Gemfile": "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\n",
        "composer.json": json.dumps({"require": {"php": ">=8", "laravel/framework": "^11"}}),
        "app/App.csproj": '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
                          "</ItemGroup></Project>",
    }
    manifests, deps = parse_manifests(files)
    names = {(d.ecosystem, d.name) for d in deps}
    for expected in [("npm", "react"), ("npm", "vite"), ("pypi", "fastapi"), ("pypi", "uvicorn"), ("pypi", "httpx"),
                     ("pypi", "rich"), ("maven", "org.x:core"), ("maven", "com.google.guava:guava"),
                     ("go", "github.com/pkg/errors"), ("cargo", "serde"), ("cargo", "tokio"),
                     ("rubygems", "rails"), ("composer", "laravel/framework"), ("nuget", "Newtonsoft.Json")]:
        assert expected in names, expected
    assert ("pypi", "python") not in names and ("composer", "php") not in names
    go_sys = next(d for d in deps if d.name == "golang.org/x/sys")
    assert go_sys.is_direct is False
    assert len(manifests) == 10 and all(m.parse_error is None for m in manifests)


def test_unparseable_manifest_is_recorded_not_dropped():
    manifests, deps = parse_manifests({"package.json": "{not json", "pom.xml": "<!DOCTYPE x [<!ENTITY a 'b'>]><p/>"})
    assert {m.manifest_path for m in manifests} == {"package.json", "pom.xml"}
    assert all(m.parse_error for m in manifests) and deps == []
    assert "DOCTYPE" in next(m for m in manifests if m.manifest_path == "pom.xml").parse_error


def test_readme_claims_extract_declarative_text_only():
    readme = ("# Project\n\n![badge](x.svg)\n\nThis service schedules deliveries using Redis queues. It runs on AWS.\n\n"
              "```bash\nnpm install\n```\n\n- Supports offline sync for field agents\n- Why?\n\n"
              "How do I run it?\n")
    claims = extract_claims(readme)
    texts = [c.text for c in claims]
    assert "This service schedules deliveries using Redis queues." in texts
    assert "Supports offline sync for field agents" in texts
    assert not any("npm install" in t or t.endswith("?") for t in texts)


def test_identity_is_exposed_in_process_and_never_serialized(tmp_path):
    """J4c: callers reuse 3.6's identity instead of rebuilding it; the serialized report still carries no email."""
    from backend.evidence.target import identity_of
    repo = tmp_path / "single"
    M.init(repo)
    M.commit(repo, {"src/app.py": M.code("app", 30)}, M.CAND, DAY.format(1, 1))
    rep = run(["o/single"], {"/repos/o/single": meta("o", "single")}, {"o/single": repo})
    ident = identity_of(rep)
    assert ident is not None and M.CAND[1] in ident.emails
    dumped = rep.model_dump_json()
    assert '"_identity"' not in dumped and M.CAND[1] not in dumped
