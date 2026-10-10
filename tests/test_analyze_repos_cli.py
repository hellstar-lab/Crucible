"""scripts/analyze_repos.py: username mode (A) and specific-repository mode (B).

Runs the real resolver and analysis pipeline end to end, with a mocked GitHub
API and local git repositories. No network, no tokens, no model calls.
"""

import importlib
import sys
from pathlib import Path

import httpx
import pytest

import make_git_fixtures as M
from backend.evidence import config
from backend.evidence.github_client import GitHubClient

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
U = "u"


def meta(owner, repo, **kw):
    return {"full_name": f"{owner}/{repo}", "name": repo, "owner": {"login": owner}, "default_branch": "main",
            "size": 10, "fork": False, "archived": False, "disabled": False, "visibility": "public",
            "clone_url": f"https://github.com/{owner}/{repo}.git", "pushed_at": "2024-06-01T00:00:00Z", **kw}


def repo_on_disk(tmp_path, name, extra_branch=None):
    repo = tmp_path / name
    M.init(repo)
    M.commit(repo, {"src/a.py": M.code("a", 10)}, M.CAND, "2024-01-01T10:00:00+00:00")
    if extra_branch:
        M._git(repo, "checkout", "-q", "-b", extra_branch)
        M.commit(repo, {"src/b.py": M.code("b", 30)}, M.CAND, "2024-01-05T10:00:00+00:00")
        M._git(repo, "checkout", "-q", "main")
    return repo


@pytest.fixture
def cli(monkeypatch):
    """Import the scripts with load_dotenv disabled (never read .env in tests)
    and route analyze_targets through a mocked API and local clones."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for mod in ("analyze_repos", "analyze"):
        sys.modules.pop(mod, None)
    analyze = importlib.import_module("analyze")
    analyze_repos = importlib.import_module("analyze_repos")
    state = {"routes": {}, "local": {}, "calls": [], "opts": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        state["calls"].append(path)
        hit = state["routes"].get(path)
        if hit is None:
            return httpx.Response(404, json={"message": "Not Found"}, headers={"x-ratelimit-remaining": "4000"})
        status, body = hit if isinstance(hit, tuple) else (200, hit)
        return httpx.Response(status, json=body, headers={"x-ratelimit-remaining": "4000"})

    real = analyze.analyze_targets

    def fake(raws, user=None, emails=None, opts=None):
        opts.allow_local, opts.use_cache = True, False
        opts.clone_url_for = {k: str(v) for k, v in state["local"].items()}
        state["opts"] = opts
        client = GitHubClient(token="", transport=httpx.MockTransport(handler))
        state["report"] = real(raws, user=user, emails=emails, opts=opts, client=client)
        return state["report"]

    monkeypatch.setattr(analyze, "analyze_targets", fake)
    state["main"] = analyze_repos.main
    return state


PROFILE = {"login": U, "id": 7, "email": M.CAND[1], "name": "U"}


# 1 + 10: username-only mode is unchanged
def test_username_only_enumerates_the_account(cli, tmp_path, capsys):
    cli["routes"] = {f"/users/{U}": PROFILE, f"/users/{U}/repos": [meta(U, "one")], f"/repos/{U}/one": meta(U, "one")}
    cli["local"] = {f"{U}/one": repo_on_disk(tmp_path, "one")}
    assert cli["main"]([U]) == 0
    out = capsys.readouterr().out
    assert f"/users/{U}/repos" in cli["calls"]
    assert f"== {U}/one  status=resolved" in out and "account mode" in out
    assert cli["opts"].max_targets == config.MAX_REPOS


# 2 + 9: username + bare --repo scans only that repository, no enumeration
def test_username_plus_bare_repo_scans_only_that_repository(cli, tmp_path, capsys):
    cli["routes"] = {f"/users/{U}": PROFILE, f"/users/{U}/repos": [meta(U, "one"), meta(U, "two")],
                     f"/repos/{U}/two": meta(U, "two")}
    cli["local"] = {f"{U}/two": repo_on_disk(tmp_path, "two")}
    assert cli["main"]([U, "--repo", "two"]) == 0
    out = capsys.readouterr().out
    assert f"/users/{U}/repos" not in cli["calls"]  # never enumerated
    assert f"/repos/{U}/one" not in cli["calls"]
    assert f"== {U}/two  status=resolved" in out and f"== {U}/one" not in out
    assert "NOT enumerated" in out and "authorship (blame): 100.0%" in out


# 3: owner/repo shorthand, no username
def test_owner_repo_shorthand(cli, tmp_path, capsys):
    cli["routes"] = {"/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": repo_on_disk(tmp_path, "r")}
    assert cli["main"](["--repo", "o/r"]) == 0
    out = capsys.readouterr().out
    assert "== o/r  status=resolved" in out and "authorship: not computed" in out  # no identity supplied


# 4: standard GitHub URLs
@pytest.mark.parametrize("url", ["https://github.com/o/r", "https://github.com/o/r.git", "http://www.github.com/o/r/",
                                 "git@github.com:o/r.git"])
def test_standard_urls(cli, tmp_path, capsys, url):
    cli["routes"] = {"/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": repo_on_disk(tmp_path, "r")}
    assert cli["main"](["--repo", url]) == 0
    assert "== o/r  status=resolved" in capsys.readouterr().out


# 5: /tree/<branch> and /blob/<branch>/<path>
def test_tree_and_blob_references(cli, tmp_path, capsys):
    cli["routes"] = {"/repos/o/r": meta("o", "r"), "/repos/o/r/branches": [{"name": "main"}]}
    cli["local"] = {"o/r": repo_on_disk(tmp_path, "r")}
    assert cli["main"](["--repo", "https://github.com/o/r/tree/main"]) == 0
    out = capsys.readouterr().out
    assert "ref requested: main | ref analysed: main (default branch) | path (whole repository)" in out
    assert cli["main"](["--repo", "https://github.com/o/r/blob/main/src/a.py"]) == 0
    out = capsys.readouterr().out
    assert "ref requested: main | ref analysed: main (default branch) | path src/a.py" in out


# 6: a branch name containing slashes
def test_branch_with_slashes_is_analysed_not_the_default(cli, tmp_path, capsys):
    cli["routes"] = {"/repos/o/r": meta("o", "r"),
                     "/repos/o/r/branches": [{"name": "main"}, {"name": "feature/new-thing"}]}
    cli["local"] = {"o/r": repo_on_disk(tmp_path, "r", extra_branch="feature/new-thing")}
    assert cli["main"](["--repo", "https://github.com/o/r/tree/feature/new-thing/src"]) == 0
    out = capsys.readouterr().out
    assert "ref requested: feature/new-thing | ref analysed: feature/new-thing | path src" in out
    assert "ref requested 'feature" not in out  # no false mismatch warning
    assert "facts: 2 source files, 40 source lines" in out  # src/b.py exists only on that branch


def test_unknown_slash_branch_falls_back_visibly(cli, tmp_path, capsys):
    cli["routes"] = {"/repos/o/r": meta("o", "r"), "/repos/o/r/branches": [{"name": "main"}]}
    cli["local"] = {"o/r": repo_on_disk(tmp_path, "r")}
    assert cli["main"](["--repo", "https://github.com/o/r/tree/feature/gone/src"]) == 0
    out = capsys.readouterr().out
    assert "ref requested: feature/gone/src | ref analysed: main (default branch) | path (whole repository)" in out
    assert "warning: ambiguous ref/path (no candidate matches a branch); fell back to default branch" in out
    assert "warning: ref requested 'feature/gone/src' but ref analysed 'main'" in out


# 7: nonexistent and inaccessible repositories
def test_missing_and_inaccessible_repositories(cli, capsys):
    cli["routes"] = {"/repos/o/private": (403, {"message": "Resource protected by organization SAML enforcement"})}
    assert cli["main"](["--repo", "o/missing", "--repo", "o/private"]) == 0
    out = capsys.readouterr().out
    assert "== o/missing  status=not_found" in out and "private repository you cannot access also returns 404" in out
    assert "== o/private  status=access_denied" in out


# 8: malformed and malicious inputs never raise
@pytest.mark.parametrize("bad", ["o/r;rm", "../etc/passwd", "o/..", "https://gitlab.com/o/r", "-oProxyCommand=x/y",
                                 "https://github.com/o/r/tree/main/../../etc", "https://user:pw@github.com/o/r"])
def test_malformed_and_malicious_inputs_are_rejected(cli, capsys, bad):
    assert cli["main"]([f"--repo={bad}"]) == 0  # "=" form so even "-..." values reach the resolver
    out = capsys.readouterr().out
    assert "status=invalid_input" in out or "INVALID" in out
    assert cli["calls"] == []  # nothing reached the API, let alone git


def test_option_looking_repo_value_is_refused_by_argparse(cli, capsys):
    with pytest.raises(SystemExit) as exc:
        cli["main"](["--repo", "-oProxyCommand=x/y"])
    assert exc.value.code == 2 and cli["calls"] == []


def test_bare_repo_name_without_username_is_an_error(cli, capsys):
    with pytest.raises(SystemExit) as exc:
        cli["main"](["--repo", "uemacs"])
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "bare repository name 'uemacs' with no owner and no username" in err
    assert "Traceback" not in err


def test_no_arguments_is_an_error(cli, capsys):
    with pytest.raises(SystemExit) as exc:
        cli["main"]([])
    assert exc.value.code == 2


# contradictory inputs are reported, not silently ignored
def test_username_and_other_owner_repo_is_stated_explicitly(cli, tmp_path, capsys):
    cli["routes"] = {f"/users/{U}": PROFILE, "/repos/other/three": meta("other", "three")}
    cli["local"] = {"other/three": repo_on_disk(tmp_path, "three")}
    assert cli["main"]([U, "--repo", "other/three"]) == 0
    out = capsys.readouterr().out
    assert f"note: other/three is owned by 'other', not '{U}'; identity comes from '{U}'" in out
    assert f"/users/{U}/repos" not in cli["calls"]


def test_resolver_context_is_explicit():
    from backend.evidence.resolve import resolve_target

    assert resolve_target("uemacs").kind == "account"  # standalone: an account
    t = resolve_target("uemacs", context="repo", default_owner="torvalds")  # behind --repo: a repo name
    assert (t.kind, t.full_name) == ("repo", "torvalds/uemacs")
    assert resolve_target("uemacs", context="repo").kind == "invalid"  # no owner: never guessed
    assert resolve_target("o/r", context="account").kind == "invalid"
    assert resolve_target("someone/else", context="repo", default_owner="me").full_name == "someone/else"


# --------------------------------------------------------------------------
# §8a additional tests
# --------------------------------------------------------------------------


def _enumeration_calls(calls):
    return sum(1 for c in calls if c.startswith("/users/") and c.endswith("/repos"))


def _zero_floats(obj, path="report"):
    """Every float equal to 0.0 anywhere in a nested structure."""
    if isinstance(obj, float) and obj == 0.0:
        return [path]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _zero_floats(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _zero_floats(v, f"{path}[{i}]")]
    return []


def two_day_repo(tmp_path, name):
    repo = tmp_path / name
    M.init(repo)
    M.commit(repo, {"src/a.py": M.code("a", 10)}, M.CAND, "2024-01-01T10:00:00+00:00")
    M.commit(repo, {"src/b.py": M.code("b", 10)}, M.BOB, "2024-01-09T10:00:00+00:00")
    return repo


def test_repo_without_identity_has_no_authorship_and_no_zero_anywhere(cli, tmp_path):
    cli["routes"] = {"/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": two_day_repo(tmp_path, "r")}
    assert cli["main"](["--repo", "o/r"]) == 0
    [t] = cli["report"].targets
    assert t.authorship is None and "no identity supplied" in t.identity_unresolved_reason
    assert t.repo_facts is not None and t.repo_facts.total_source_lines == 20
    assert cli["report"].input_spec.identity_source is None
    assert _zero_floats(t.model_dump()) == []  # None means not measured: never a fabricated 0.0


def test_user_with_repo_of_another_owner_records_both(cli, tmp_path, capsys):
    cli["routes"] = {f"/users/{U}": PROFILE, "/repos/other/three": meta("other", "three")}
    cli["local"] = {"other/three": two_day_repo(tmp_path, "three")}
    assert cli["main"]([U, "--repo", "other/three"]) == 0
    report = cli["report"]
    [t] = report.targets
    assert t.repo_facts.full_name == "other/three"
    assert report.input_spec.user == U and report.input_spec.identity_source == "user"
    assert t.authorship.identity_used["logins"] == [U]
    assert t.authorship.authored_ratio == 0.5  # identity from U: the candidate's half
    assert _enumeration_calls(cli["calls"]) == 0


@pytest.mark.parametrize("argv", [[U, "--repo", "two"], [U, "--repo", "o/r"], ["--repo", "o/r"],
                                  ["--repo", "https://github.com/o/r"], [U, "--repo", "o/r", "--repo", "two"]])
def test_enumeration_endpoint_is_never_called_with_repo(cli, tmp_path, argv):
    cli["routes"] = {f"/users/{U}": PROFILE, f"/users/{U}/repos": [meta(U, "two")], "/repos/o/r": meta("o", "r"),
                     f"/repos/{U}/two": meta(U, "two")}
    cli["local"] = {"o/r": two_day_repo(tmp_path, "r"), f"{U}/two": two_day_repo(tmp_path, "two")}
    assert cli["main"](argv) == 0
    assert _enumeration_calls(cli["calls"]) == 0  # counted on the mock, not inferred from output


def test_email_takes_precedence_over_username(cli, tmp_path):
    cli["routes"] = {f"/users/{U}": PROFILE, "/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": two_day_repo(tmp_path, "r")}
    assert cli["main"]([U, "--repo", "o/r", "--email", M.BOB[1]]) == 0
    report = cli["report"]
    assert report.input_spec.identity_source == "email"
    assert f"/users/{U}" not in cli["calls"]  # username not used for identity when --email is given
    assert report.targets[0].authorship.authored_ratio == 0.5  # Bob's half


def test_duplicates_collapse_and_are_reported(cli, tmp_path, capsys):
    cli["routes"] = {"/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": two_day_repo(tmp_path, "r")}
    assert cli["main"](["--repo", "o/r", "--repo", "https://github.com/O/R", "--repo", "git@github.com:o/r.git"]) == 0
    statuses = [t.status for t in cli["report"].targets]
    assert statuses.count("resolved") == 1 and statuses.count("duplicate") == 2
    assert len(cli["report"].input_spec.duplicates) == 2


def test_max_targets_is_honoured(cli, tmp_path):
    cli["routes"] = {f"/repos/o/r{i}": meta("o", f"r{i}") for i in range(3)}
    cli["local"] = {f"o/r{i}": two_day_repo(tmp_path, f"r{i}") for i in range(3)}
    assert cli["main"](["--repo", "o/r0", "--repo", "o/r1", "--repo", "o/r2", "--max-targets", "2"]) == 0
    over = [t for t in cli["report"].targets if t.status == "invalid_input"]
    assert len(over) == 1 and "over max_targets (2)" in over[0].message


def test_single_commit_repo_is_confidence_none(cli, tmp_path):
    repo = tmp_path / "one"
    M.init(repo)
    M.commit(repo, {"src/a.py": M.code("a", 40)}, M.CAND, "2024-01-01T10:00:00+00:00")
    cli["routes"] = {f"/users/{U}": PROFILE, f"/repos/{U}/one": meta(U, "one")}
    cli["local"] = {f"{U}/one": repo}
    assert cli["main"]([U, "--repo", "one"]) == 0
    a = cli["report"].targets[0].authorship
    assert a.authorship_confidence == "none" and a.authored_ratio_meaningful is False and a.authored_ratio == 1.0


def test_oversized_repo_degrades_not_skips(cli, capsys):
    big = meta("o", "big", size=400 * 1024)  # 400 MiB packed, over the 300 MiB default
    commits = [{"sha": f"{i:040x}", "commit": {"author": {"date": f"2024-0{i}-01T00:00:00Z"}, "message": f"c{i}"}}
               for i in (1, 2)]
    cli["routes"] = {f"/users/{U}": PROFILE, "/repos/o/big": big, "/repos/o/big/commits": commits,
                     "/repos/o/big/git/trees/main": {"tree": [], "truncated": False},
                     **{f"/repos/o/big/commits/{c['sha']}": {"files": [{"filename": "a.c"}]} for c in commits}}
    assert cli["main"]([U, "--repo", "o/big"]) == 0
    [t] = cli["report"].targets
    assert t.mode == "api_commits_only" and t.authorship is not None  # degraded, NOT skipped
    a = t.authorship
    assert a.mode == "api_commits_only" and a.commit_count == 2 and a.files_touched == ["a.c"]
    assert a.authored_ratio is None and a.authored_source_lines is None and a.total_source_lines is None
    assert t.ref_analyzed == "main"


@pytest.mark.parametrize("bad", ["owner/repo; rm -rf /", "../../etc/passwd"])
def test_malicious_repo_never_reaches_git(cli, monkeypatch, bad):
    import subprocess

    from backend.evidence import gitutil

    seen = []
    real_popen = subprocess.Popen

    def recording_popen(args, *a, **kw):
        seen.append((args, kw.get("shell")))
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(gitutil.subprocess, "Popen", recording_popen)
    assert cli["main"]([f"--repo={bad}"]) == 0
    [t] = cli["report"].targets
    assert t.status == "invalid_input"
    assert seen == [] and cli["calls"] == []  # rejected before git and before the API


def test_git_only_ever_receives_argument_lists(cli, tmp_path, monkeypatch):
    import subprocess

    from backend.evidence import gitutil

    seen = []
    real_popen = subprocess.Popen

    def recording_popen(args, *a, **kw):
        seen.append((args, kw.get("shell")))
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(gitutil.subprocess, "Popen", recording_popen)
    cli["routes"] = {"/repos/o/r": meta("o", "r")}
    cli["local"] = {"o/r": two_day_repo(tmp_path, "r")}
    assert cli["main"](["--repo", "o/r"]) == 0
    assert seen, "expected git invocations"
    for args, shell in seen:
        assert isinstance(args, list) and all(isinstance(a, str) for a in args) and args[0] == "git"
        assert shell is not True


def test_no_shell_true_anywhere_in_evidence_code():
    root = Path(__file__).resolve().parent.parent / "backend" / "evidence"
    for src in root.glob("*.py"):
        text = src.read_text()
        assert "shell=True" not in text and "os.system(" not in text, src.name
