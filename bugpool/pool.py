"""generate_pool: the offline batch job. Runs in a temporary git worktree of the target repo, so the real working tree
is never touched; writes one pool file per repo."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import AUTHORSHIP_UNKNOWN, COULD_NOT_RUN, COVERAGE_INSUFFICIENT, INSUFFICIENT, coverage_map, docs_check
from . import burned
from . import claims as claim_mod
from .baseline import precondition, run_suite, stability
from .burned import Registry
from .candidates import enumerate_candidates, filter_candidates
from .validate import CLEAN, validate

POOL_VERSION = 1
SECOND_TIER_NOTE = "planned second tier, not built: characterization tests for authored code that has no behaviour test"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _empty(pool: dict, reason: str, refusal: str = COVERAGE_INSUFFICIENT, **detail) -> dict:
    """An empty pool. ``refusal`` is the class (COULD_NOT_RUN, COVERAGE_INSUFFICIENT, AUTHORSHIP_UNKNOWN), ``reason``
    the specific cause; a suite that could not run is never reported as insufficient coverage."""
    message = INSUFFICIENT if refusal == COVERAGE_INSUFFICIENT else refusal
    pool.update(status="empty", candidates=[], refusal=refusal, reason=reason, message=message, detail=detail)
    return pool


def authorship(repo: Path, sources: list[str]) -> dict:
    """How far blame can tell authors apart: the commits and distinct author emails that touch ``sources``. One
    commit (a squash, an upload) attributes every line to its committer, whoever wrote it."""
    log = _git(repo, "log", "--format=%H %ae", "--", *sources).splitlines()
    return {"commits": len(log), "authors": sorted({ln.split(" ", 1)[1].lower() for ln in log if " " in ln}),
            "established": len(log) > 1}


def generate_pool(repo: Path, emails: list[str], *, source: str = "backend", python: str | None = None,
                  behaviour_modules: int = 7, target: int = 20, max_validations: int = 300, seed: int = 0,
                  max_per_killing_test: int = 2, max_per_module: int = 3, burned_registry: Path | None = None,
                  time_budget_s: float = 3 * 3600, second_tier=None, baseline_runs: int = 3, kill_runs: int = 3,
                  single_commit_override: str | None = None, hidden_dirs: tuple[str, ...] = ("docs",), claims=None,
                  log=print) -> dict:
    """A pool of pre-validated bug candidates for ``repo`` at HEAD, or an EMPTY pool with the refusal recorded.

    ``source``: the directory to mutate, or several, comma-separated ("app,workload").
    ``python``: the interpreter that runs the repository's tests (its own venv); bugpool's tools come from this one.
    ``single_commit_override``: blame cannot attribute lines when the sources have a single commit, so such a repo is
    refused unless a reason is given; the reason is recorded in the pool, verbatim.
    ``claims``: the Phase 4 claim-verdict record. Candidates in modules the résumé claims are validated FIRST,
    strongest claim first (bugpool.claims), and every pooled candidate is marked with its claim targeting.
    ``second_tier`` is the seam for the planned characterization-test fallback (not built). When the pool would be
    empty it is called as second_tier(worktree, coverage_map, steps) and must return pool candidates."""
    repo = Path(repo).resolve()
    python = python or sys.executable
    sources = [x.strip().rstrip("/") for x in source.split(",") if x.strip()]
    cand_emails = {e.lower() for e in emails}
    pool = {"version": POOL_VERSION, "repo": str(repo), "head_sha": _git(repo, "rev-parse", "HEAD"), "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "identity_emails": sorted(cand_emails),
            "params": {"behaviour_modules": behaviour_modules, "target": target, "max_validations": max_validations, "seed": seed,
                       "max_per_killing_test": max_per_killing_test, "max_per_module": max_per_module,
                       "baseline_runs": baseline_runs, "kill_runs": kill_runs, "hidden_dirs": list(hidden_dirs)},
            "second_tier": SECOND_TIER_NOTE if second_tier is None else "supplied", "stats": {}}
    tmp = Path(tempfile.mkdtemp(prefix="bugpool-wt-"))
    wt = tmp / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    try:
        t0 = time.monotonic()
        missing = [x for x in sources if not (wt / x).exists()]
        if missing:
            return _empty(pool, f"no {missing[0]!r} directory to mutate")
        auth = authorship(wt, sources)
        pool["authorship"] = auth
        if not auth["established"]:
            if not single_commit_override:
                return _empty(pool, f"the sources have {auth['commits']} commit(s): blame attributes every line to its committer, "
                                    "so authored code cannot be told apart from a teammate's", refusal=AUTHORSHIP_UNKNOWN,
                              commits=auth["commits"], authors=auth["authors"])
            auth["override"] = {"accepted": True, "reason": single_commit_override,
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            log(f"authorship: {auth['commits']} commit(s); override recorded")
        # 1. the precondition: run the suite once, unmodified; "could not run" is its own refusal, with the error
        runs = [run_suite(wt, python)]
        pre = precondition(runs[0])
        pool["precondition"] = pre
        if pre["status"] == "could_not_run":
            log(f"precondition: could not run the suite: {pre['error'][:300]}")
            return _empty(pool, pre["error"], refusal=COULD_NOT_RUN, exit_code=pre["exit_code"])
        log(f"precondition: the suite ran: {pre['collected']} collected, {pre['passed']} passed, {pre['failed']} failed, "
            f"{pre['errors']} errors, {len(pre['skipped'])} skipped ({pre['seconds']}s)")
        if pre["collected"] == 0:
            return _empty(pool, "the repository has no tests", tests_collected=0)
        # 2. the baseline stability gate: N identical runs, or the test is not an oracle
        for _ in range(baseline_runs - 1):
            r = run_suite(wt, python)
            if not r.ran:
                return _empty(pool, f"a baseline re-run could not run: {r.error}", refusal=COULD_NOT_RUN)
            runs.append(r)
        stab = stability(runs)
        oracle = set(stab["oracle"])
        pool["oracle"] = {k: v for k, v in stab.items() if k != "oracle"} | {"size": len(oracle)}
        log(f"baseline: {baseline_runs} runs; oracle {len(oracle)} tests; unstable {len(stab['unstable'])}; "
            f"not passing at baseline {len(stab['not_passing_at_baseline'])}")
        t_cov = time.monotonic()
        cmap = coverage_map.build(wt, sources, python)
        if cmap.error or cmap.exit_code in (2, 3, 4):
            return _empty(pool, f"the coverage run could not run (exit {cmap.exit_code}): {cmap.error}", refusal=COULD_NOT_RUN)
        pool["stats"]["tests_collected"] = cmap.collected
        pool["stats"]["coverage_seconds"] = round(time.monotonic() - t_cov, 1)
        widths = sorted((len(m) for t, m in cmap.modules.items() if t in oracle), reverse=True)
        pool["stats"]["modules_per_oracle_test"] = {"max": widths[0] if widths else 0, "median": widths[len(widths) // 2] if widths else 0,
                                                    "at_least_threshold": sum(1 for w in widths if w >= behaviour_modules)}
        log(f"coverage: {cmap.collected} tests, {len(cmap.modules)} test functions with coverage of {source}; "
            f"modules per oracle test {pool['stats']['modules_per_oracle_test']}")
        behaviour = cmap.behaviour_tests(behaviour_modules) & oracle
        pool["stats"]["behaviour_tests"] = len(behaviour)
        if not behaviour:
            return _empty(pool, f"no behaviour test: no oracle test touches >= {behaviour_modules} modules of {source}",
                          tests_with_coverage=len(cmap.modules))
        cands, err = enumerate_candidates(wt, sources)
        if err:
            return _empty(pool, "candidate enumeration failed", refusal=COULD_NOT_RUN, **err)
        kept, steps = filter_candidates(wt, cands, cmap, lambda e: e.lower() in cand_emails, behaviour_modules, oracle)
        # unseen discipline (permanent): a line already shown is never pooled again
        reg = Registry(burned_registry or repo / burned.DEFAULT)
        before = len(kept)
        src_lines = {c.path: (wt / c.path).read_text().splitlines() for c in kept}
        kept = [c for c in kept if not reg.burned(c.path, c.start[0], src_lines[c.path][c.start[0] - 1])]
        steps["not_burned"] = len(kept)
        pool["unseen_discipline"] = {"registry": str(reg.path), "burned_lines": len(reg), "excluded_candidates": before - len(kept),
                                     "rule": "a line shown to a participant, in a report or in a commit message is never pooled"}
        pool["stats"]["filters"] = steps
        log(f"candidates: {steps}")
        if not kept:
            if second_tier is not None:
                pool["candidates"] = second_tier(wt, cmap, steps)
                pool["status"] = "ok" if pool["candidates"] else "empty"
                return pool
            return _empty(pool, "no authored line is covered by a behaviour test", filters=steps)
        order = random.Random(seed).sample(kept, len(kept))
        if claims is not None:  # claimed modules first; seeded random order within a rank (a stable sort)
            cites = claim_mod.citations(claims)
            order.sort(key=lambda c: tuple(claim_mod.target_for(c.path, c.start[0], cites).get("sort_key") or [claim_mod.UNCITED, True, 9]))
        seen_lines, out, tried = set(), [], {}
        per_test: dict[str, int] = {}
        per_module: dict[str, int] = {}
        diversity_dropped = 0
        for c in order:
            if len(out) >= target or sum(tried.values()) >= max_validations or time.monotonic() - t0 > time_budget_s:
                break
            if (c.path, c.start[0]) in seen_lines:
                continue  # one candidate per line
            if per_module.get(c.path, 0) >= max_per_module:
                continue  # diversity: this module is full; skip before paying for validation
            seen_lines.add((c.path, c.start[0]))
            v = validate(wt, c, python, kill_runs=kill_runs)
            tried[v.cls] = tried.get(v.cls, 0) + 1
            log(f"  [{sum(tried.values())}] {v.cls:<18} {c.family:<18} {c.path}:{c.start[0]} ({v.seconds}s)")
            if v.cls != CLEAN:
                continue
            f = v.failed[0]
            if per_test.get(f["test"], 0) >= max_per_killing_test:
                diversity_dropped += 1  # diversity: this killing test already anchors its share of the pool
                log(f"      dropped for diversity: {f['test']} already kills {per_test[f['test']]}")
                continue
            per_test[f["test"]] = per_test.get(f["test"], 0) + 1
            per_module[c.path] = per_module.get(c.path, 0) + 1
            minus = next((ln[1:] for ln in v.diff.splitlines() if ln.startswith("-") and not ln.startswith("---")), "")
            plus = next((ln[1:] for ln in v.diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")), "")
            out.append({"id": None, "path": c.path, "line": c.start[0], "end_line": c.end[0], "occurrence": c.occurrence,
                        "diff": v.diff, "original_line": minus,
                        "mutated_line": plus, "operator": c.operator, "family": c.family, "killing_test": f["test"],
                        "killing_message": f["message"], "covering_tests": c.covering_tests, "behaviour_tests": c.behaviour_tests,
                        "validation_seconds": v.seconds, "kill_runs": v.kill_runs,
                        "checks": {"clean_kill": True, "exception_at_site": False, "telegraph_hits": v.telegraph_hits,
                                   "plausibility": {"whitelisted_family": c.family,
                                                    "integer_literal_only": True,  # floats / complex never enumerated
                                                    "lint_clean": True,  # no new ruff diagnostic on the line
                                                    "format_stable": True}}})  # the formatter leaves the mutation as is
        pool["stats"]["validated"] = tried
        pool["stats"]["rejected_unstable"] = tried.get("unstable", 0)
        pool["stats"]["dropped_for_diversity"] = diversity_dropped
        pool["stats"]["spread"] = {"killing_tests": per_test, "modules": per_module}
        pool["stats"]["seconds"] = round(time.monotonic() - t0, 1)
        if not out:
            if second_tier is not None:
                out = second_tier(wt, cmap, steps)
            if not out:
                return _empty(pool, f"no candidate validated as a clean, non-telegraphing kill ({sum(tried.values())} tried)",
                              validated=tried)
        from .session import candidate_id
        passages = docs_check.corpus(wt)
        for cand in out:
            cand["id"] = candidate_id(cand)
            cand["checks"]["docs_telegraph"] = docs_check.check(wt, cand, passages, hidden_dirs)
        dt = [c["checks"]["docs_telegraph"] for c in out]
        pool["stats"]["docs_telegraph"] = {"described": sum(d["described"] for d in dt),
                                           "described_in_hidden_dirs": sum(d["hits_in_hidden_dirs"] > 0 for d in dt),
                                           "described_in_visible_prose": sum(d["hits_visible_in_both"] > 0 for d in dt),
                                           "of": len(dt), "rule": "recorded, never used to reject"}
        pool.update(status="ok", candidates=out, refusal=None, reason=None, message=None)
        if claims is not None:
            pool = claim_mod.annotate(pool, claims, str(claims) if isinstance(claims, (str, Path)) else None)
            if not pool["claim_targeting"]["candidates_in_claimed_modules"]:
                pool["claim_targeting"]["message"] = claim_mod.NO_CLAIMED_MODULE
        return pool
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def write(pool: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(pool, indent=1))
