"""Phase 3: GitHub fetch and authorship attribution. Offline: synthetic git
repositories with known authors, and a mocked GitHub API (zero real calls)."""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

import make_git_fixtures as M
from backend.evidence import analyze_github_detailed, analyze_local_repo, config
from backend.evidence.blame import merge_ranges, parse_line_porcelain
from backend.evidence.clone import cloned_repo
from backend.evidence.errors import (GitHubError, GitHubNetworkError, GitHubNotFound, GitHubRateLimited,
                                     GitTimeout)
from backend.evidence.exclusions import GitAttributes, has_source_map, is_minified, path_reason
from backend.evidence.github_client import GitHubClient
from backend.evidence.gitutil import run_git
from backend.evidence.identity import IdentitySet, build_identity_set, parse_noreply
from backend.evidence.repo_signals import DAY, clustered, has_tests


@pytest.fixture
def identity() -> IdentitySet:
    ident = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)
    ident.add(M.CAND[1], "profile_email", M.CAND[0])
    return ident


# --------------------------------------------------------------------------
# fixtures are reproducible
# --------------------------------------------------------------------------


def test_fixture_shas_are_identical_across_builds_and_ignore_user_git_config(tmp_path, monkeypatch):
    first = M.build("shared_repo", tmp_path / "a")
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text("[core]\n\tautocrlf = true\n[init]\n\tdefaultBranch = master\n"
                       "[user]\n\temail = someone@else.example\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    second = M.build("shared_repo", tmp_path / "b")
    sha = lambda r: subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout  # noqa: E731
    branch = lambda r: subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=r, capture_output=True,  # noqa: E731
                                      text=True).stdout.strip()
    assert sha(first) == sha(second)
    assert branch(second) == "main"


# --------------------------------------------------------------------------
# exact ground truth per fixture
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", [n for n in M.EXPECTED if n != "empty_repo"])
def test_fixture_matches_ground_truth_exactly(git_fixtures, identity, name):
    r = analyze_local_repo(git_fixtures[name], identity, name)
    got = r.model_dump()
    expected = M.EXPECTED[name]
    for key, value in expected.items():
        if key in got:
            assert got[key] == value, f"{name}.{key}: expected {value!r}, got {got[key]!r}"
    if "ranges" in expected:
        ranges = {f.path: f.authored_ranges for f in r.files}
        for path, want in expected["ranges"].items():
            assert ranges[path] == want
    if "authored_paths" in expected:
        assert {f.path for f in r.files} == expected["authored_paths"]
    assert r.builds is None  # Phase 3 cannot know; never a fabricated False


def test_shared_repo_split_is_exactly_40_35_25(git_fixtures, identity):
    for who, lines in ((M.CAND, 40), (M.BOB, 35), (M.CAROL, 25)):
        ident = IdentitySet(login="nobody")
        ident.add(who[1], "profile_email")
        r = analyze_local_repo(git_fixtures["shared_repo"], ident, "shared_repo")
        assert r.authored_lines == lines and r.code_lines == 100


def test_files_list_only_contains_authored_files(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["shared_repo"], identity, "shared_repo")
    assert [f.path for f in r.files] == ["src/core.py"]
    assert all(f.authored_lines > 0 for f in r.files)


def test_vendored_generated_docs_binary_excluded_from_ratio(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["vendored_repo"], identity, "vendored_repo")
    assert r.authored_ratio == 1.0  # Bob's node_modules, lock file, minified, dist, gen, pb2 all excluded
    assert {f.path for f in r.files} == M.EXPECTED["vendored_repo"]["source_paths"]


def test_mailmap_unifies_aliases_and_rejects_same_name_impostor(git_fixtures):
    ident = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)  # knows only the noreply address
    r = analyze_local_repo(git_fixtures["mailmap_repo"], ident, "mailmap_repo")
    assert r.authored_lines == 30 and r.authored_ratio == 0.5
    assert "d.py" not in {f.path for f in r.files}  # "Alex Sample" with another email is not the candidate


def test_committer_email_recorded_alongside_author(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["shared_repo"], identity, "shared_repo")
    [core] = r.files
    assert core.committer_emails == {"ale...@example.com": 40}  # redacted like every stored email


def test_empty_repo_is_skipped_with_reason(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["empty_repo"], identity, "empty_repo")
    assert r.skip_reason == "no commits" and r.files == []


def test_oversized_file_is_skipped_and_recorded(git_fixtures, identity, monkeypatch):
    monkeypatch.setattr(config, "MAX_BLAME_FILE_BYTES", 100)
    r = analyze_local_repo(git_fixtures["solo_repo"], identity, "solo_repo")
    assert r.source_files == 0 and r.code_lines is None and r.authored_ratio is None  # nothing countable
    assert r.excluded_files_by_reason["too_large"] == 2
    assert r.excluded_lines_by_reason["too_large"] == 63  # raw lines of both files, still reported


# --------------------------------------------------------------------------
# blame parsing and line ranges
# --------------------------------------------------------------------------


def test_merge_ranges_bridges_blank_only_gaps():
    assert merge_ranges([1, 2, 3, 5, 6, 9], bridgeable={4}) == [(1, 6), (9, 9)]  # 4 = blank/comment
    assert merge_ranges([1, 3], bridgeable=set()) == [(1, 1), (3, 3)]
    assert merge_ranges([], bridgeable={1}) == []


def test_parse_line_porcelain_reads_author_and_committer_per_line():
    sha = "a" * 40
    out = (f"{sha} 1 1 2\nauthor A\nauthor-mail <A@X.COM>\ncommitter C\ncommitter-mail <c@x.com>\n"
           f"summary s\nfilename f\n\tcode\n{sha} 2 2\nauthor A\nauthor-mail <a@x.com>\ncommitter C\n"
           f"committer-mail <c@x.com>\nsummary s\nfilename f\n\t   \n").encode()
    lines = parse_line_porcelain(out)
    assert [(ln.number, ln.author_email, ln.committer_email, ln.blank) for ln in lines] == [
        (1, "a@x.com", "c@x.com", False), (2, "a@x.com", "c@x.com", True)]


# --------------------------------------------------------------------------
# exclusions: vendored Linguist data, two content heuristics, .gitattributes
# --------------------------------------------------------------------------


def test_linguist_data_loads_all_186_patterns():
    from backend.evidence import exclusions as exc

    assert len(exc.VENDOR_PATTERNS) == 168 and len(exc.DOCUMENTATION_PATTERNS) == 18


def test_bad_linguist_pattern_fails_loudly(tmp_path):
    from backend.evidence.exclusions import LinguistPatternError, load_patterns

    (tmp_path / "bad.yml").write_text("- (^|/)ok/\n- (unclosed\n- '[z-a]'\n")
    with pytest.raises(LinguistPatternError) as err:
        load_patterns("bad.yml", tmp_path)
    assert "(unclosed" in str(err.value) and "[z-a]" in str(err.value) and "2 pattern(s)" in str(err.value)


@pytest.mark.parametrize("path,expected", [
    ("node_modules/x/index.js", "vendored_path"), ("vendor/lib.go", "vendored_path"),
    ("dist/bundle.js", "vendored_path"), ("third_party/miniz/miniz.c", "vendored_path"),
    ("backend/mvnw", "vendored_path"), ("backend/mvnw.cmd", "vendored_path"), ("gradlew", "vendored_path"),
    ("static/app.min.js", "vendored_path"), ("css/site.min.css", "vendored_path"),
    ("README.md", "documentation_path"), ("docs/index.rst", "documentation_path"),
    ("src/app.py", None), ("main.go", None), ("CMakeLists.txt", None),
])
def test_linguist_path_rules(path, expected):
    assert path_reason(path) == expected


@pytest.mark.parametrize("path", [
    # DOCUMENTED GAPS: Linguist's vendor/documentation data does not cover these.
    # Lock files and protoc output are still excluded later by pygount (name list /
    # "DO NOT EDIT" header); the rest are counted as source. See the Phase 3.5 report.
    "venv/lib/x.py", ".venv/x.py", "lib/python3.11/site-packages/x.py", "build/out.js", "target/classes/A.java",
    "app/migrations/0001_initial.py", "data/raw/podcast_8_raw.json", "api/user_pb2.py", "package-lock.json",
])
def test_paths_not_covered_by_linguist_path_data(path):
    assert path_reason(path) is None


@pytest.mark.parametrize("path,text,expected", [
    ("a.js", ("x" * 111 + "\n") * 3, False),  # avg = 333 // 4 = 83 with the trailing "" line
    ("a.js", "x" * 111, True),  # one line, avg 111 > 110
    ("a.js", "x" * 110, False),  # avg exactly 110 is not > 110
    ("a.css", "y" * 300 + "\n" + "y" * 300, True),
    ("a.py", "z" * 500, False),  # only .js / .css
])
def test_is_minified(path, text, expected):
    assert is_minified(path, text) is expected


@pytest.mark.parametrize("text,expected", [
    ("var a = 1;\n//# sourceMappingURL=a.js.map\n", True),
    ("var a = 1;\n//# sourceMappingURL=a.js.map", True),
    ("var a = 1;\n//@ sourceMappingURL=a.js.map\n", True),
    ("body{}\n/*# sourceMappingURL=a.css.map */\n", True),
    ("eval(x)\n//# sourceURL=dyn.js\n", True),
    ("//# sourceMappingURL=a.js.map\nvar a = 1;\nvar b = 2;\n", False),  # not in the last two lines
    ("var a = 1; // mentions sourceMappingURL in a comment\n", False),
])
def test_has_source_map(text, expected):
    assert has_source_map("a.js", text) is expected
    assert has_source_map("a.py", text) is False


@pytest.mark.parametrize("attrs,path,expected", [
    ("gen/** linguist-generated", "gen/api.py", "generated"),
    ("*.py linguist-vendored", "src/a.py", "vendored_path"),
    ("docs/** linguist-documentation", "docs/conf.py", "documentation_path"),
    ("vendor/ours.py -linguist-vendored", "vendor/ours.py", None),
    ("vendor/** !linguist-vendored", "vendor/x.go", None),
    ("node_modules/** linguist-vendored=false", "node_modules/a.js", None),
    ("*.js linguist-generated\nsrc/keep.js -linguist-generated", "src/keep.js", None),  # later line wins
    ("*.js -linguist-generated", "app.min.js", "vendored_path"),  # unmarking generated leaves vendored
])
def test_gitattributes_overrides(attrs, path, expected):
    assert path_reason(path, GitAttributes.parse(attrs)) == expected


def test_vendored_fixture_every_file_has_expected_reason(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["vendored_repo"], identity, "vendored_repo")
    reasons = {e.path: e.reason for e in r.top_excluded_files}
    counted = {f.path for f in r.files}
    for path, expected in M.REASONS.items():
        if expected is None:
            assert path in counted, path
        else:
            assert reasons.get(path) == expected, path


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("email,expected", [
    ("12345678+alex-sample-demo@users.noreply.github.com", (12345678, "alex-sample-demo")),
    ("alex-sample-demo@users.noreply.github.com", (None, "alex-sample-demo")),
    ("12345678+Alex-Sample-Demo@USERS.NOREPLY.GITHUB.COM", (12345678, "Alex-Sample-Demo")),
    ("alex.sample@example.com", None),
    ("12345678+alex@users.noreply.github.com.evil.example", None),
])
def test_parse_noreply(email, expected):
    assert parse_noreply(email) == expected


def test_identity_matching_rules(identity):
    assert identity.evidence_for("alex.sample@example.com") == ["profile_email"]
    assert identity.evidence_for("ALEX.SAMPLE@EXAMPLE.COM") == ["profile_email"]
    assert identity.matches("12345678+alex-sample-demo@users.noreply.github.com")
    assert identity.matches("alex-sample-demo@users.noreply.github.com")  # legacy form
    assert not identity.matches("99999999+alex-sample-demo@users.noreply.github.com")  # wrong numeric id
    assert not identity.matches("12345678+someone-else@users.noreply.github.com")
    assert not identity.matches("alex.sample@impostor.example")  # same NAME elsewhere is never enough


def _mock_client(routes: dict, calls: list | None = None) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        key = request.url.path
        if key not in routes:
            return httpx.Response(404, json={"message": "Not Found"})
        status, body, headers = routes[key] if isinstance(routes[key], tuple) else (200, routes[key], {})
        return httpx.Response(status, json=body, headers=headers)
    return GitHubClient(token="", transport=httpx.MockTransport(handler))


PROFILE = {"login": M.LOGIN, "id": M.USER_ID, "email": "alex.sample@example.com", "name": "Alex Sample"}


def test_build_identity_set_uses_three_signals_with_evidence():
    repo = {"name": "proj", "owner": {"login": M.LOGIN}}
    commits = [
        {"sha": "a1" * 20, "author": {"login": M.LOGIN},
         "commit": {"author": {"name": "Alex S", "email": "alex@work.example"}}},
        {"sha": "b2" * 20, "author": None,  # GitHub could not link it: not evidence
         "commit": {"author": {"name": "Alex Sample", "email": "unlinked@example.com"}}},
        {"sha": "c3" * 20, "author": {"login": "someone-else"},
         "commit": {"author": {"name": "Alex Sample", "email": "other@example.com"}}},
    ]
    client = _mock_client({f"/users/{M.LOGIN}": PROFILE, f"/repos/{M.LOGIN}/proj/commits": commits})
    ident = build_identity_set(M.LOGIN, client, repos=[repo])
    assert ident.evidence_for("alex@work.example") == [f"github_commit_login:proj@{'a1' * 6}"]
    assert ident.evidence_for("alex.sample@example.com") == ["profile_email"]
    assert ident.evidence_for(f"{M.USER_ID}+{M.LOGIN}@users.noreply.github.com") == ["noreply"]
    assert not ident.matches("unlinked@example.com")
    assert not ident.matches("other@example.com")
    assert ("Alex S", "alex@work.example") in ident.pairs()


# --------------------------------------------------------------------------
# GitHub client (mocked transport)
# --------------------------------------------------------------------------


def test_client_paginates_via_link_header():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        link = {} if page == 3 else {"link": f'<https://api.github.com/users/u/repos?page={page + 1}>; rel="next"'}
        return httpx.Response(200, json=[{"name": f"r{page}"}], headers={"x-ratelimit-remaining": str(100 - page), **link})
    client = GitHubClient(token="", transport=httpx.MockTransport(handler))
    assert [r["name"] for r in client.list_repos("u")] == ["r1", "r2", "r3"]
    assert client.calls_made == 3 and client.rate_limit_remaining == 97


def test_client_404_rate_limit_retry_after_network_and_empty_repo():
    from backend.evidence.errors import GitHubAuthError

    with pytest.raises(GitHubAuthError):
        _mock_client({"/users/u": (401, {"message": "Bad credentials"}, {})}).get_user("u")
    client = _mock_client({"/users/ghost": (404, {}, {})})
    with pytest.raises(GitHubNotFound):
        client.get_user("ghost")
    limited = _mock_client({"/users/u": (403, {"message": "rate limited"},
                                         {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1999999999"})})
    with pytest.raises(GitHubRateLimited) as exc:
        limited.get_user("u")
    assert exc.value.reset_at == 1999999999 and exc.value.remaining == 0
    secondary = _mock_client({"/users/u": (429, {}, {"retry-after": "30"})})
    with pytest.raises(GitHubRateLimited) as exc:
        secondary.get_user("u")
    assert exc.value.reset_at is not None
    forbidden = _mock_client({"/users/u": (403, {"message": "forbidden"}, {"x-ratelimit-remaining": "50"})})
    with pytest.raises(GitHubError):
        forbidden.get_user("u")

    def boom(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(GitHubNetworkError):
        GitHubClient(token="", transport=httpx.MockTransport(boom)).get_user("u")
    empty = _mock_client({"/repos/u/e/commits": (409, {"message": "Git Repository is empty."}, {})})
    assert empty.list_commits("u", "e") == []


# --------------------------------------------------------------------------
# clone, timeouts, cleanup
# --------------------------------------------------------------------------


def test_clone_is_full_history_and_cleaned_up(git_fixtures):
    with cloned_repo(str(git_fixtures["shared_repo"]), allow_local=True) as repo:
        kept = repo
        assert run_git(["rev-list", "--count", "HEAD"], cwd=repo).strip() == b"3"
        assert run_git(["rev-parse", "--is-shallow-repository"], cwd=repo).strip() == b"false"
    assert not kept.exists() and not kept.parent.exists()


def test_clone_cleans_up_on_failure(git_fixtures):
    with pytest.raises(RuntimeError):
        with cloned_repo(str(git_fixtures["solo_repo"]), allow_local=True) as repo:
            kept = repo
            raise RuntimeError("analysis crashed")
    assert not kept.parent.exists()


def test_local_clone_refused_unless_allowed(git_fixtures):
    from backend.evidence.errors import GitError

    with pytest.raises(GitError):
        with cloned_repo(f"file://{git_fixtures['solo_repo']}"):
            pass


def test_git_timeout_kills_process():
    with pytest.raises(GitTimeout):
        run_git(["slp"], extra_config=("-c", "alias.slp=!sleep 5"), timeout=0.5)


# --------------------------------------------------------------------------
# repo signals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("author,committer,expected", [
    ([0, 400 * DAY], [10, 70], (True, None)),  # rewrite: spread authors, minutes of commits
    ([0, 400 * DAY], [0, 400 * DAY], (False, None)),  # normal history
    ([0, 10 * DAY], [0, 3600], (True, None)),  # 1h / 10d = 0.42% < 1%
    ([0, 20 * DAY], [0, 6 * 3600], (False, None)),  # 6h / 20d = 1.25% >= 1%
    ([0, 7 * DAY], [0, 7 * DAY * 0.0099], (True, None)),  # boundary: exactly 7 days is evaluated
    ([0, 6 * DAY], [0, 60], (None, "insufficient_history")),  # under 7 days: not evaluated
    ([5], [5], (None, "insufficient_history")),  # single commit
    ([5, 5], [5, 900], (None, "insufficient_history")),  # author span 0
    ([], [], (None, "insufficient_history")),
])
def test_clustered_rule(author, committer, expected):
    assert clustered(author, committer) == expected


def test_short_history_records_insufficient_history(tmp_path, identity):
    repo = tmp_path / "short"
    M.init(repo)
    M.commit(repo, {"a.py": M.code("a", 5)}, M.CAND, "2024-01-01T10:00:00+00:00")
    M.commit(repo, {"b.py": M.code("b", 5)}, M.CAND, "2024-01-04T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "short")
    assert r.committer_dates_clustered is None
    assert "committer_dates_clustered not evaluated: insufficient_history" in r.notes


def test_skipped_repo_reports_none_never_zero(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["empty_repo"], identity, "empty_repo")
    assert r.skip_reason == "no commits"
    for field in ("authored_ratio", "source_files", "total_lines", "code_lines", "authored_lines",
                  "commit_span_days", "commit_count", "contributor_count", "largest_commit_share",
                  "author_date_span_days", "committer_date_span_days"):
        assert getattr(r, field) is None, f"{field} must be None (not measured), got {getattr(r, field)!r}"
    for flag in ("single_commit_import", "committer_dates_clustered", "builds", "tests_present"):
        assert getattr(r, flag) is None


def test_repo_without_source_lines_has_ratio_none(tmp_path, identity):
    repo = tmp_path / "docs_only"
    M.init(repo)
    M.commit(repo, {"README.md": M.code("readme", 5)}, M.CAND, "2024-01-01T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "docs_only")
    assert r.skip_reason is None and r.source_files == 0
    assert r.authored_ratio is None and r.source_lines is None  # nothing countable: not measured, not 0%
    assert r.excluded_files_by_reason["documentation_path"] == 1


@pytest.mark.parametrize("paths,expected", [
    (["src/a.py", "tests/test_a.py"], True), (["test_x.py"], True), (["pkg/x_test.go"], True),
    (["web/a.test.ts"], True), (["src/test/java/ATest.java"], True), (["src/a.py", "README.md"], False),
])
def test_has_tests(paths, expected):
    assert has_tests(paths) is expected


# --------------------------------------------------------------------------
# analyze_github end to end, mocked API + local clones
# --------------------------------------------------------------------------


def test_analyze_github_selection_caps_and_contract(git_fixtures, monkeypatch):
    import backend.evidence as ev

    def repo(name, fork=False, pushed="2025-01-01T00:00:00Z", size=10):
        return {"name": name, "fork": fork, "pushed_at": pushed, "size": size, "language": "Python",
                "owner": {"login": M.LOGIN}, "clone_url": str(git_fixtures.get(name, git_fixtures["solo_repo"]))}

    repos = [repo("shared_repo", pushed="2025-03-01T00:00:00Z"), repo("solo_repo", pushed="2025-02-01T00:00:00Z"),
             repo("forked", fork=True, pushed="2025-06-01T00:00:00Z"), repo("huge", size=10**9),
             repo("empty_repo", pushed="2024-01-01T00:00:00Z")]
    calls: list[str] = []
    client = _mock_client({f"/users/{M.LOGIN}": PROFILE, f"/users/{M.LOGIN}/repos": repos}, calls)
    real = ev.cloned_repo
    monkeypatch.setattr(ev, "cloned_repo", lambda url: real(url, allow_local=True))
    result = analyze_github_detailed(M.LOGIN, max_repos=4, client=client)
    by = {r.name: r for r in result.repos}
    assert [r.name for r in result.repos][:4] == ["shared_repo", "solo_repo", "huge", "empty_repo"]  # non-forks first
    assert by["forked"].skip_reason == "beyond max_repos (4)"  # fork sorted after non-forks
    assert "over MAX_REPO_MB" in by["huge"].skip_reason
    assert by["empty_repo"].skip_reason == "no commits"
    for skipped in (by["huge"], by["forked"], by["empty_repo"]):
        assert skipped.authored_ratio is None and skipped.total_lines is None and skipped.source_files is None
    assert by["shared_repo"].authored_ratio == 0.4 and by["solo_repo"].authored_ratio == 1.0
    assert all(c.startswith("https://api.github.com/") for c in calls)
    from backend.contracts import EvidenceReport

    EvidenceReport(candidate_id="synthetic", repositories=result.repos, findings=[])


def test_unknown_user_raises_not_found():
    with pytest.raises(GitHubNotFound):
        analyze_github_detailed("ghost", client=_mock_client({}))


def test_evidence_package_never_imports_the_model_router():
    import re

    # Real import statements only: the technology lexicon may legitimately *name* OpenAI.
    forbidden = re.compile(r"^\s*(?:from\s+(?:backend\.llm|backend\s+import\s+llm|openai)\b|"
                           r"import\s+(?:openai|backend\.llm)\b)", re.M)
    pkg = Path(__file__).resolve().parent.parent / "backend" / "evidence"
    for src in pkg.glob("*.py"):
        assert not forbidden.search(src.read_text()), f"{src.name} imports the model router or a model SDK"


# --------------------------------------------------------------------------
# live (one real GitHub run)
# --------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to call GitHub")
def test_live_analyze_small_public_user():
    result = analyze_github_detailed("octocat", max_repos=1)
    assert result.api_calls > 0
    analysed = [r for r in result.repos if r.skip_reason is None]
    assert len(analysed) == 1
    r = analysed[0]
    assert 0.0 <= r.authored_ratio <= 1.0 and r.commit_count and r.commit_count > 0
    assert r.builds is None


# --------------------------------------------------------------------------
# unlinked identities: unmatched authors and the identity-gap fact
# --------------------------------------------------------------------------

OWNED = {"owner": {"login": M.LOGIN}, "fork": False}


def test_owner_under_unlinked_email_sets_identity_gap(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["identity_gap_repo"], identity, "identity_gap_repo", OWNED)
    assert r.authored_ratio == 0.0
    assert r.likely_identity_gap is True
    top = r.unmatched_authors[0]
    assert (top.email_redacted, top.name, top.authored_lines, top.commit_count) == (
        "ale...@<local-machine>", "Alex Sample", 90, 1)
    assert top.reason == "user.email not configured on the committing machine"
    assert [a.authored_lines for a in r.unmatched_authors] == [90, 10]


def test_genuine_team_repo_is_not_an_identity_gap(git_fixtures):
    stranger = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)  # matches nobody in shared_repo
    r = analyze_local_repo(git_fixtures["shared_repo"], stranger, "shared_repo", OWNED)
    assert r.authored_ratio == 0.0
    assert r.likely_identity_gap is False  # top unmatched author has 40%, not > 80%
    assert [a.authored_lines for a in r.unmatched_authors] == [40, 35, 25]


@pytest.mark.parametrize("meta,expected", [
    ({"owner": {"login": M.LOGIN}, "fork": True}, False),  # a fork is never an identity gap
    ({"owner": {"login": "someone-else"}, "fork": False}, False),  # not owned by the candidate
    ({}, None),  # ownership unknown: not evaluated
])
def test_identity_gap_requires_owned_non_fork(git_fixtures, identity, meta, expected):
    r = analyze_local_repo(git_fixtures["identity_gap_repo"], identity, "identity_gap_repo", meta)
    assert r.likely_identity_gap is expected


def test_identity_gap_false_when_candidate_has_any_lines(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["shared_repo"], identity, "shared_repo", OWNED)
    assert r.authored_ratio == 0.4 and r.likely_identity_gap is False


def test_unmatched_authors_redacted_sorted_capped():
    from backend.evidence import redact_email, unmatched_authors

    assert redact_email("auratech704@gmail.com") == "aur...@gmail.com"
    assert redact_email("ab@x.org") == "ab...@x.org"
    for machine in ("abhinav@abhinav-ka-macbook-air-2.local", "dev@box.lan", "u@nas.home", "u@ci.internal",
                    "root@localhost", "me@my-laptop", "me@host.localdomain"):
        assert redact_email(machine).endswith("...@<local-machine>"), machine
    assert redact_email("me@company.co.uk") == "me...@company.co.uk"  # routable domains keep recognisability
    lines = {f"user{i:02d}@corp.example": 100 - i for i in range(15)}
    out = unmatched_authors(lines, {"user00@corp.example": 7}, {"user00@corp.example": "U Zero"})
    assert len(out) == 10
    assert [a.authored_lines for a in out] == sorted((a.authored_lines for a in out), reverse=True)
    assert out[0].email_redacted == "use...@corp.example" and out[0].commit_count == 7 and out[0].name == "U Zero"
    assert all("@" in a.email_redacted and "..." in a.email_redacted for a in out)
    dumped = json.dumps([a.model_dump() for a in out])
    assert "user00@corp.example" not in dumped  # no full third-party email is stored


def test_skipped_repo_has_no_identity_facts(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["empty_repo"], identity, "empty_repo", OWNED)
    assert r.unmatched_authors == [] and r.likely_identity_gap is None


def test_machine_hostname_never_appears_in_output(git_fixtures, identity):
    r = analyze_local_repo(git_fixtures["identity_gap_repo"], identity, "identity_gap_repo", OWNED)
    dumped = r.model_dump_json()
    for leak in ("alexs-macbook-pro-7", "macbook", ".local", M.UNLINKED[1]):
        assert leak not in dumped, f"{leak!r} leaked into the RepoInfo output"
    assert "<local-machine>" in dumped


def test_skipped_repo_via_api_reports_tests_present_none(git_fixtures, monkeypatch):
    import backend.evidence as ev

    repos = [{"name": "huge", "fork": False, "pushed_at": "2025-01-01T00:00:00Z", "size": 10**9,
              "language": "C", "owner": {"login": M.LOGIN}, "clone_url": "unused"}]
    client = _mock_client({f"/users/{M.LOGIN}": PROFILE, f"/users/{M.LOGIN}/repos": repos})
    [r] = analyze_github_detailed(M.LOGIN, client=client).repos
    assert r.skip_reason and r.tests_present is None and r.authored_ratio is None


# --------------------------------------------------------------------------
# Phase 3.5: exclusion fixture (real files on disk) and pygount counting
# --------------------------------------------------------------------------

EXCLUSION_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "exclusion_repo"


@pytest.fixture
def exclusion_repo(tmp_path):
    import shutil

    repo = tmp_path / "exclusion_repo"
    M.init(repo)
    shutil.copytree(EXCLUSION_FIXTURE, repo, dirs_exist_ok=True)
    M.commit(repo, {}, M.CAND, "2024-01-01T10:00:00+00:00")
    return repo


def test_exclusion_fixture_counts_every_category_exactly(exclusion_repo, identity):
    r = analyze_local_repo(exclusion_repo, identity, "exclusion_repo")
    assert r.excluded_files_by_reason == {
        "vendored_path": 2,  # node_modules/leftpad/index.js, vendor/lib.py
        "documentation_path": 1,  # docs/guide.md
        "generated": 0,
        "minified": 1,  # static/bundle.js (average line length 112 > 110)
        "source_map": 1,  # static/mapped.js
        "duplicate": 1,  # src/app_copy.py, byte-identical to src/app.py
        "unparseable": 0, "binary": 0, "empty": 0, "too_large": 0,
    }
    assert r.excluded_lines_by_reason == {
        "vendored_path": 3 + 2, "documentation_path": 3, "generated": 0, "minified": 2, "source_map": 4,
        "duplicate": 11, "unparseable": 0, "binary": 0, "empty": 0, "too_large": 0,
    }
    reasons = {e.path: e.reason for e in r.top_excluded_files}
    assert reasons["src/app_copy.py"] == "duplicate" and "src/app.py" not in reasons  # first copy counts
    # state_info names the original, repo-relative (never a local temp path)
    assert [(s.path, s.state, s.state_info) for s in r.skipped_files] == [("src/app_copy.py", "duplicate", "src/app.py")]


def test_exclusion_fixture_source_lines_use_code_plus_string(exclusion_repo, identity):
    r = analyze_local_repo(exclusion_repo, identity, "exclusion_repo")
    files = {f.path: f for f in r.files}
    q = files["src/queries.py"]  # heavy docstring + embedded SQL
    assert (q.code_lines, q.documentation_lines, q.string_lines, q.empty_lines) == (2, 7, 5, 2)
    assert q.total_lines == 16 and q.source_lines == 7 and q.authored_lines == 7
    a = files["src/app.py"]
    assert (a.code_lines, a.documentation_lines, a.string_lines, a.empty_lines) == (6, 1, 0, 4)
    assert a.source_lines == 6 and a.authored_lines == 6
    # repo totals: only the two countable files
    assert r.source_files == 2
    assert (r.code_lines, r.documentation_lines, r.string_lines, r.empty_lines) == (8, 8, 5, 6)
    assert r.total_lines == 27 and r.source_lines == 13 and r.authored_lines == 13 and r.authored_ratio == 1.0


def test_authored_ranges_bridge_comments_and_docstrings(exclusion_repo, identity):
    r = analyze_local_repo(exclusion_repo, identity, "exclusion_repo")
    files = {f.path: f for f in r.files}
    # queries.py source lines are 9 (def), 11 (return) and 12-16 (SQL); the docstring
    # on line 10 is not authored source but sits inside the function, so it is bridged.
    assert files["src/queries.py"].authored_ranges == [(9, 16)]
    assert files["src/app.py"].authored_ranges == [(1, 11)]


def test_comment_lines_are_not_credited_as_authored(tmp_path, identity):
    repo = tmp_path / "comments"
    M.init(repo)
    M.commit(repo, {"a.py": b"x = 1\n"}, M.CAND, "2024-01-01T10:00:00+00:00")
    M.commit(repo, {"a.py": b"x = 1\n# a long explanatory comment\n# and another one\n"}, M.BOB,
             "2024-01-02T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "comments")
    assert r.source_lines == 1 and r.authored_lines == 1 and r.authored_ratio == 1.0  # Bob's comments: not code
    assert r.documentation_lines == 2


def test_pygount_generated_names_and_headers_are_excluded(tmp_path, identity):
    repo = tmp_path / "gen"
    M.init(repo)
    M.commit(repo, {"package-lock.json": b'{\n  "lockfileVersion": 3\n}\n',
                    "client/api.py": b"# NOTE: This class is auto generated by OpenAPI Generator.\n"
                                     b"# Do not edit the class manually.\nX = 1\n",
                    "src/app.py": b"x = 1\n"}, M.CAND, "2024-01-01T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "gen")
    assert r.excluded_files_by_reason["generated"] == 2 and r.source_files == 1
    assert {s.path for s in r.skipped_files} == {"package-lock.json", "client/api.py"}


def test_zero_countable_files_reports_none_not_zero(tmp_path, identity):
    repo = tmp_path / "all_vendored"
    M.init(repo)
    M.commit(repo, {"node_modules/a/index.js": b"x = 1;\n", "README.md": b"# hi\n"}, M.CAND,
             "2024-01-01T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "all_vendored")
    assert r.source_files == 0
    for field in ("authored_ratio", "total_lines", "code_lines", "documentation_lines", "string_lines",
                  "empty_lines", "source_lines", "authored_lines", "largest_commit_share"):
        assert getattr(r, field) is None, field
    assert r.excluded_files_by_reason["vendored_path"] == 1 and r.excluded_files_by_reason["documentation_path"] == 1


def test_utf8_source_with_multibyte_characters_is_counted(tmp_path, identity):
    # Regression: pygount's automatic encoding fell back to cp1252 and dropped
    # UTF-8 files containing emoji as unparseable.
    repo = tmp_path / "utf8"
    M.init(repo)
    M.commit(repo, {"app.py": "print('✅ done — ünïcode 🚀')\nx = 1\n".encode(),
                    "legacy.py": "name = 'caf\xe9'\n".encode("cp1252")}, M.CAND, "2024-01-01T10:00:00+00:00")
    r = analyze_local_repo(repo, identity, "utf8")
    assert r.excluded_files_by_reason["unparseable"] == 0
    assert r.source_files == 2 and r.source_lines == 3 and r.authored_ratio == 1.0
