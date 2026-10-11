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

from . import INSUFFICIENT, coverage_map
from .candidates import enumerate_candidates, filter_candidates
from .validate import CLEAN, validate

POOL_VERSION = 1
SECOND_TIER_NOTE = "planned second tier, not built: characterization tests for authored code that has no behaviour test"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _empty(pool: dict, reason: str, **detail) -> dict:
    pool.update(status="empty", candidates=[], reason=reason, message=INSUFFICIENT, detail=detail)
    return pool


def generate_pool(repo: Path, emails: list[str], *, source: str = "backend", python: str | None = None,
                  behaviour_modules: int = 7, target: int = 20, max_validations: int = 300, seed: int = 0,
                  time_budget_s: float = 3 * 3600, second_tier=None, log=print) -> dict:
    """A pool of pre-validated bug candidates for ``repo`` at HEAD, or an EMPTY pool with the reason recorded.

    ``second_tier`` is the seam for the planned characterization-test fallback (not built). When the pool would be
    empty it is called as second_tier(worktree, coverage_map, steps) and must return pool candidates."""
    repo = Path(repo).resolve()
    python = python or sys.executable
    cand_emails = {e.lower() for e in emails}
    pool = {"version": POOL_VERSION, "repo": str(repo), "head_sha": _git(repo, "rev-parse", "HEAD"), "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "identity_emails": sorted(cand_emails),
            "params": {"behaviour_modules": behaviour_modules, "target": target, "max_validations": max_validations, "seed": seed},
            "second_tier": SECOND_TIER_NOTE if second_tier is None else "supplied", "stats": {}}
    tmp = Path(tempfile.mkdtemp(prefix="bugpool-wt-"))
    wt = tmp / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    try:
        t0 = time.monotonic()
        if not (wt / source).exists():
            return _empty(pool, f"no {source!r} directory to mutate")
        cmap = coverage_map.build(wt, source, python)
        pool["stats"]["tests_collected"] = cmap.collected
        pool["stats"]["coverage_seconds"] = round(time.monotonic() - t0, 1)
        log(f"coverage: {cmap.collected} tests, {len(cmap.modules)} test functions with coverage of {source}/")
        if cmap.collected == 0:
            return _empty(pool, "the repository has no tests", tests_collected=0)
        behaviour = cmap.behaviour_tests(behaviour_modules)
        pool["stats"]["behaviour_tests"] = len(behaviour)
        if not behaviour:
            return _empty(pool, f"no behaviour test: none touches >= {behaviour_modules} modules of {source}/",
                          tests_with_coverage=len(cmap.modules))
        cands, err = enumerate_candidates(wt, source, Path(python))
        if err:
            return _empty(pool, "candidate enumeration failed", **err)
        kept, steps = filter_candidates(wt, cands, cmap, lambda e: e.lower() in cand_emails, behaviour_modules)
        pool["stats"]["filters"] = steps
        log(f"candidates: {steps}")
        if not kept:
            if second_tier is not None:
                pool["candidates"] = second_tier(wt, cmap, steps)
                pool["status"] = "ok" if pool["candidates"] else "empty"
                return pool
            return _empty(pool, "no authored line is covered by a behaviour test", filters=steps)
        order = random.Random(seed).sample(kept, len(kept))
        seen_lines, out, tried = set(), [], {}
        for c in order:
            if len(out) >= target or sum(tried.values()) >= max_validations or time.monotonic() - t0 > time_budget_s:
                break
            if (c.path, c.start[0]) in seen_lines:
                continue  # one candidate per line
            seen_lines.add((c.path, c.start[0]))
            v = validate(wt, c, python)
            tried[v.cls] = tried.get(v.cls, 0) + 1
            log(f"  [{sum(tried.values())}] {v.cls:<18} {c.family:<18} {c.path}:{c.start[0]} ({v.seconds}s)")
            if v.cls != CLEAN:
                continue
            f = v.failed[0]
            minus = next((ln[1:] for ln in v.diff.splitlines() if ln.startswith("-") and not ln.startswith("---")), "")
            plus = next((ln[1:] for ln in v.diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")), "")
            out.append({"path": c.path, "line": c.start[0], "end_line": c.end[0], "diff": v.diff, "original_line": minus,
                        "mutated_line": plus, "operator": c.operator, "family": c.family, "killing_test": f["test"],
                        "killing_message": f["message"], "covering_tests": c.covering_tests, "behaviour_tests": c.behaviour_tests,
                        "validation_seconds": v.seconds,
                        "checks": {"clean_kill": True, "exception_at_site": False, "telegraph_hits": v.telegraph_hits,
                                   "plausibility": {"whitelisted_family": c.family,
                                                    "integer_literal_only": True}}})  # floats were filtered out before validation
        pool["stats"]["validated"] = tried
        pool["stats"]["seconds"] = round(time.monotonic() - t0, 1)
        if not out:
            if second_tier is not None:
                out = second_tier(wt, cmap, steps)
            if not out:
                return _empty(pool, f"no candidate validated as a clean, non-telegraphing kill ({sum(tried.values())} tried)",
                              validated=tried)
        pool.update(status="ok", candidates=out, reason=None, message=None)
        return pool
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def write(pool: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(pool, indent=1))
