"""Phase 4B evidence retrieval. Deterministic, zero model calls: llm.call is replaced by a function that fails the test,
and NEBIUS_API_KEY is absent (conftest). Scenario repos are built at test time by make_git_fixtures.RETRIEVAL_BUILDERS
and analysed through the real 3.6 pipeline (mocked GitHub API), with the snapshot cache in a temp dir."""

from __future__ import annotations

import os

import pytest

import make_git_fixtures as M
import test_target as T
from backend import llm, retrieval
from backend.contracts import Dependency, EvidenceItem
from backend.evidence import cache
from backend.evidence.identity import IdentitySet
from backend.retrieval import config as rconfig
from backend.retrieval import rank, rules
from backend.retrieval.sources import ExclusionViolation, assert_eligible, load_view


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a model call leaked into the deterministic retrieval path")
    monkeypatch.setattr(llm, "call", boom)
    assert "NEBIUS_API_KEY" not in os.environ


def identity() -> IdentitySet:
    ident = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)
    ident.add(M.CAND[1], "profile_email")
    return ident


def build_scenarios(tmp) -> dict:
    """{name: (TargetReport, repo path, cache root)} for every retrieval scenario (also used by tests/test_verdicts.py)."""
    out = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cache, "CACHE_DIR", tmp / "cache")
        for name, build in M.RETRIEVAL_BUILDERS.items():
            repo = tmp / name
            build(repo)
            routes = {f"/repos/{M.LOGIN}/{name}": T.meta(M.LOGIN, name)}
            raw = f"{M.LOGIN}/{name}"
            if name == "monorepo":
                routes[f"/repos/{M.LOGIN}/{name}/branches"] = [{"name": "main"}]
                raw = f"https://github.com/{M.LOGIN}/{name}/tree/main/packages/api"
            profile_routes = {f"/users/{M.LOGIN}": {**T.PROFILE, "email": None}} if name == "identity_gap" else {}
            report = T.run([raw], {**routes, **profile_routes}, {f"{M.LOGIN}/{name}": repo}, use_cache=True).targets[0]
            assert report.status == "resolved"
            out[name] = (report, repo, tmp / "cache")
    return out


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    return build_scenarios(tmp_path_factory.mktemp("retrieval"))


def view(scenarios, name, checkout=True, ident=identity):
    report, repo, root = scenarios[name]
    return load_view(report, snapshot_root=root, checkout=repo if checkout else None, identity=ident() if ident else None)


def claim(text, kind="technology_use", cid="c1", checkable="yes", secondary=None):
    return {"claim_id": cid, "kind": kind, "checkable_from_code": checkable, "text": text, "secondary_kinds": secondary or []}


REDIS = claim("Built a caching layer with Redis")


# --------------------------------------------------------------------------
# the six scenarios
# --------------------------------------------------------------------------


def test_clean_small_finds_the_real_dependency_and_its_import(scenarios):
    es = retrieval.retrieve(REDIS, [view(scenarios, "clean_small")])
    assert es.retrieval_performed and es.searched.technologies == ["redis"]
    got = {(i.rule, i.path, i.start_line, i.excerpt) for i in es.items}
    assert ("r_dependency", "requirements.txt", 1, "redis==5.0.1") in got
    assert ("r_import", "src/cache.py", 1, "import redis") in got
    imp = next(i for i in es.items if i.rule == "r_import")
    assert imp.authored_by_candidate is True and imp.authored_ratio == 1.0 and "tree-sitter" in imp.match_detail


def test_exclusion_stress_technology_only_in_excluded_code_gives_zero_items(scenarios):
    v = view(scenarios, "exclusion_stress")
    es = retrieval.retrieve(REDIS, [v])
    assert es.items == [] and es.retrieval_performed
    assert es.searched.repos and es.searched.rules_fired  # we looked: this is evidence of absence, not absence of looking
    assert es.searched.excluded_files_skipped >= 6
    # the technology really is in the repository, only in excluded files
    excluded = v.excluded_paths
    for p in ("vendor/redis_client/client.py", "node_modules/ioredis/index.js", "gen/cache_pb2.py", "static/cache.min.js",
              "docs/caching.md", "third_party/redis.conf"):
        assert p in excluded, p
    # and no rule of any kind ever returns an excluded path
    for kind, text in [("technology_use", "Used Redis, Node.js and Python"), ("ownership", "Wrote the redis client"),
                       ("artifact_exists", "Wrote docs and tests"), ("metric", "Cut latency 50%")]:
        for it in retrieval.retrieve(claim(text, kind), [v]).items:
            assert it.path not in excluded


def test_single_commit_import_never_claims_authorship(scenarios):
    v = view(scenarios, "single_commit_import")
    assert v.authorship_confidence == "none"
    es = retrieval.retrieve(REDIS, [v])
    assert es.items and all(i.authored_by_candidate is None for i in es.items)
    imp = next(i for i in es.items if i.rule == "r_import")
    assert imp.authored_ratio == 1.0 and "authorship_confidence none" in imp.authorship_caveat  # reported, not trusted


def test_readme_only_looks_and_finds_nothing(scenarios):
    es = retrieval.retrieve(REDIS, [view(scenarios, "readme_only")])
    assert es.items == [] and es.searched is not None and es.searched.source_files == 0
    assert es.searched.repos == [f"{M.LOGIN}/readme_only"] and "r_dependency" in es.searched.rules_fired


def test_monorepo_scope_keeps_evidence_inside_the_path(scenarios):
    v = view(scenarios, "monorepo")
    assert v.scoped_path == "packages/api"
    react = retrieval.retrieve(claim("Built the dashboard in React"), [v])
    assert react.items == [] and react.searched is not None
    api = retrieval.retrieve(claim("Built the API with FastAPI"), [v])
    assert api.items and all(i.path.startswith("packages/api/") for i in api.items if i.path)


def test_identity_gap_caveat_is_carried_onto_every_item(scenarios):
    v = view(scenarios, "identity_gap")
    assert v.report.authorship.authored_ratio == 0.0 and v.likely_identity_gap is True
    es = retrieval.retrieve(REDIS, [v])
    assert es.items
    for it in es.items:
        assert it.likely_identity_gap is True and "likely_identity_gap" in it.authorship_caveat


# --------------------------------------------------------------------------
# one test per rule
# --------------------------------------------------------------------------


def test_r_dependency_exact_and_pinned_fuzzy_cutoff(scenarios):
    v = view(scenarios, "clean_small")
    res = rules.r_dependency("Redis", ["redis"], v)
    assert [(i.match, i.path) for i in res.items] == [("exact", "requirements.txt")]
    deps = [Dependency(manifest_path="requirements.txt", ecosystem="pypi", name="rediss")]
    v.report = v.report.model_copy(update={"repo_facts": v.report.repo_facts.model_copy(update={"dependencies": deps})})
    fuzzy = rules.r_dependency("Redis", ["redis"], v).items
    assert fuzzy and fuzzy[0].match == "fuzzy" and f"cutoff {rconfig.DEP_FUZZY_CUTOFF}" in fuzzy[0].match_detail
    deps = [Dependency(manifest_path="requirements.txt", ecosystem="pypi", name="requests")]
    v.report = v.report.model_copy(update={"repo_facts": v.report.repo_facts.model_copy(update={"dependencies": deps})})
    assert rules.r_dependency("Redis", ["redis"], v).items == []


def test_r_import_records_the_parser_path(scenarios, monkeypatch):
    ts = rules.r_import("Redis", ["redis"], view(scenarios, "clean_small")).items
    assert ts[0].match_detail.endswith("parser=tree-sitter") and ts[0].start_line == 1
    monkeypatch.setattr(rules, "_ts_import_lines", lambda *a: None)
    rx = rules.r_import("Redis", ["redis"], view(scenarios, "clean_small")).items
    assert rx[0].match_detail.endswith("parser=line-regex") and rx[0].start_line == 1
    no_checkout = rules.r_import("Redis", ["redis"], view(scenarios, "clean_small", checkout=False)).items
    assert no_checkout[0].start_line is None and no_checkout[0].excerpt is None and "no checkout" in no_checkout[0].match_detail


def test_r_config_finds_kubernetes_and_docker_which_are_not_dependencies(scenarios):
    v = view(scenarios, "clean_small")
    res = rules.r_config("Deployed it on Kubernetes with Docker", ["kubernetes", "docker"], v)
    found = {(i.technology, i.path, i.match) for i in res.items}
    assert ("docker", "Dockerfile", "pattern") in found
    assert ("kubernetes", "k8s/deployment.yaml", "pattern") in found
    kinds = [i for i in res.items if i.technology == "kubernetes" and i.kind == "config"]
    assert kinds and kinds[0].excerpt == "kind: Deployment"


def test_r_ownership_matches_named_artifacts_and_keeps_authorship_honest(scenarios):
    v = view(scenarios, "clean_small")
    ledger = rules.r_ownership("Designed the ledger reconciliation module", [], v).items
    assert any(i.kind == "function" and i.path == "src/billing/ledger.py" for i in ledger)
    assert all(i.authored_by_candidate is False for i in ledger if i.path == "src/billing/ledger.py")  # Bob wrote it
    cache_items = rules.r_ownership("Built the cache client", [], v).items
    assert any(i.path == "src/cache.py" and i.authored_by_candidate is True for i in cache_items)
    assert rules.r_ownership("Built it", [], v).outcomes == ["r_ownership:no_artifact_named"]


def test_r_structure_tests_and_ci(scenarios):
    v = view(scenarios, "clean_small")
    res = rules.r_structure("Wrote unit tests and a CI pipeline", [], v)
    assert set(res.structure_keys) >= {"test_dirs", "ci_files"}
    paths = {i.path for i in res.items}
    assert "tests/test_cache.py" in paths
    # Linguist's vendor.yml treats .github/ as vendored, so 3.5 excludes the CI workflow: not evidence, and said so
    assert ".github/workflows/ci.yml" in v.excluded_paths and ".github/workflows/ci.yml" not in paths
    assert "r_structure:excluded_by_3.5=1" in res.outcomes


def test_r_scale_reports_both_numbers_and_decides_nothing(scenarios):
    res = rules.r_scale("Maintained 300k lines of code", [], view(scenarios, "clean_small"))
    [it] = res.items
    assert it.kind == "count" and it.claimed_value == "300k" and it.observed_value.startswith("total_source_lines: ")
    assert it.authored_by_candidate is None and it.path is None


def test_r_history_overlap_is_a_fact(scenarios):
    from datetime import date
    res = rules.r_history("Worked on it from Jan 2024 to Mar 2024", [], view(scenarios, "clean_small"), today=date(2026, 10, 11))
    [it] = res.items
    assert "first_commit 2024-01-01" in it.observed_value and "2024-01-01 .. 2024-03-31" in it.claimed_value
    assert it.match_detail.startswith("overlap ")
    assert rules.r_history("Worked on it", [], view(scenarios, "clean_small")).outcomes == ["r_history:no_date_in_claim"]


def test_r_benchmark_found_and_absent(scenarios):
    found = rules.r_benchmark("Cut p95 latency by 40%", [], view(scenarios, "clean_small")).items
    assert [i.path for i in found] == ["benchmarks/bench_cache.py"]
    es = retrieval.retrieve(claim("Cut p95 latency by 40%", "metric"), [view(scenarios, "single_commit_import")])
    assert es.items == [] and "no_benchmark_present" in es.searched.rules_fired


@pytest.mark.parametrize("kind", ["role_or_team", "outcome", "other"])
def test_kinds_not_knowable_from_code_are_not_searched(scenarios, kind):
    es = retrieval.retrieve(claim("Led a team of 6 engineers", kind, checkable="no"), [view(scenarios, "clean_small")])
    assert es.retrieval_performed is False and es.skip_reason and es.searched is None and es.items == []


# --------------------------------------------------------------------------
# invariants, ranking, version
# --------------------------------------------------------------------------


def test_assert_eligible_rejects_an_excluded_path(scenarios):
    v = view(scenarios, "exclusion_stress")
    bad = EvidenceItem(repo_full_name=v.full_name, path="vendor/redis_client/client.py", kind="import", rule="r_import")
    with pytest.raises(ExclusionViolation):
        assert_eligible(v, [bad])


def test_without_identity_nothing_is_attributed(scenarios):
    es = retrieval.retrieve(REDIS, [view(scenarios, "clean_small", ident=None)])
    assert es.items and all(i.authored_by_candidate is None for i in es.items)


def test_an_identity_that_does_not_reproduce_the_report_attributes_nothing(scenarios):
    def wrong():
        ident = IdentitySet(login="someone-else")
        ident.add("nobody@example.org", "profile_email")
        return ident
    v = view(scenarios, "clean_small", ident=wrong)
    es = retrieval.retrieve(REDIS, [v])
    assert all(i.authored_by_candidate is None for i in es.items) and any("identity not reproduced" in n for n in es.notes)


def test_rank_caps_at_ten_with_exact_and_authored_first():
    items = [EvidenceItem(repo_full_name="o/r", path=f"f{i}.py", kind="file_presence", rule="r_config", match="fuzzy" if i % 2 else "exact",
                          authored_by_candidate=(i % 3 == 0)) for i in range(15)]
    top, dropped = rank.rank("python files", items)
    assert len(top) == 10 and dropped == 5
    tiers = [(0 if it.match == "fuzzy" else 1, {True: 2, None: 1, False: 0}[it.authored_by_candidate]) for it in top]
    assert tiers == sorted(tiers, reverse=True)


def test_retrieval_is_deterministic_and_versioned(scenarios):
    a = retrieval.retrieve(REDIS, [view(scenarios, "clean_small")]).model_dump_json()
    b = retrieval.retrieve(REDIS, [view(scenarios, "clean_small")]).model_dump_json()
    assert a == b and rconfig.retrieval_version().startswith("4B.1+")


def test_cli_end_to_end(scenarios, tmp_path, capsys, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path
    report, repo, root = scenarios["clean_small"]
    (tmp_path / "claims.json").write_text(json.dumps({"claims": [REDIS, claim("Led a team of 6", "role_or_team", "c2", "no")]}))
    (tmp_path / "repos.json").write_text(json.dumps({"targets": [json.loads(report.model_dump_json())]}))
    spec = importlib.util.spec_from_file_location("retrieve_evidence", Path(__file__).resolve().parent.parent / "scripts" / "retrieve_evidence.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr("sys.argv", ["retrieve_evidence.py", "--claims", str(tmp_path / "claims.json"), "--evidence", str(tmp_path / "repos.json"),
                                     "--json", str(tmp_path / "out.json"), "--user", M.LOGIN, "--email", M.CAND[1],
                                     "--checkout", f"{report.repo_facts.full_name}={repo}", "--snapshot-root", str(root)])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "c1" in out and "authored_by_candidate=2" in out and "NOT SEARCHED: role_or_team is not knowable from code" in out
    data = json.loads((tmp_path / "out.json").read_text())
    assert data[0]["items"][0]["excerpt"] in ("redis==5.0.1", "import redis") and data[1]["searched"] is None
