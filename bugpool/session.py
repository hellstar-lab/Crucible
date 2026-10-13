"""The timed session: plant ONE pre-validated bug from a pool in a throwaway worktree, let the participant hunt it with
the same four actions the AI stranger has, and write a session record. No search for bugs happens here.

Lifecycle:
  1. an EMPTY pool is refused with its recorded reason; nothing is planted;
  2. one candidate is chosen and planted in a throwaway git worktree; the participant's real tree is never touched;
  3. the participant sees the file tree and the failing test's name and output; nothing else (no diff, no hint, no
     module name);
  4. every action is logged with a timestamp and a type: FILE_OPEN, SEARCH, TEST_RUN, ANSWER (bugpool.tools.ACTIONS,
     the stranger's exact set);
  5. one ANSWER (file + line) ends the hunt; running out of the action budget is a valid outcome, not a failure;
  6. the worktree is destroyed and a record is written.

The record reports actions-to-find for the participant and, from the stranger's budget curve on the SAME candidate,
for the stranger, and stops there. No score, no rank, no hire signal. Elapsed time is recorded, never the headline,
never compared.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import INSUFFICIENT, burned, tools
from . import claims as claim_mod
from .fingerprint import PlantError, check_diff


class ShieldError(RuntimeError):
    """Claim text from the résumé reached something the participant or the stranger is shown."""


def leaked_claims(text: str, withheld: list[str], wt: Path | None = None, min_len: int = 20) -> list[str]:
    """Claim texts (long enough to identify a claim) that appear in ``text`` and are NOT the repository's own words:
    a sentence the repo itself contains is repo content, not a leak from the résumé."""
    out = []
    for t in withheld:
        if t and len(t) >= min_len and t in text:
            if wt is not None and tools.search(wt, re.escape(t)) != "no matches":
                continue
            out.append(t)
    return out

DEFAULT_BUDGET = 40  # actions before the answer, the stranger's ceiling


def candidate_id(c: dict) -> str:
    return f"{c['path']}:{c['line']}:{c['operator'].split('/')[-1]}:{hashlib.sha256(c['diff'].encode()).hexdigest()[:8]}"


def _load(pool):
    return json.loads(Path(pool).read_text()) if isinstance(pool, (str, Path)) else pool


def plant(pool, workdir: Path, *, seed: int | None = None, candidate: dict | None = None, form_check: bool = True) -> dict:
    """{"planted": bool, "message": str, "candidate": dict | None}. ``workdir`` is a checkout at the pool's commit.
    Raises PlantError, LOUDLY and before touching anything, when the pooled diff would be findable by its form."""
    pool = _load(pool)
    if pool.get("status") != "ok" or not pool.get("candidates"):
        return {"planted": False, "message": INSUFFICIENT, "reason": pool.get("reason"), "candidate": None}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True).stdout.strip()
    if head != pool["head_sha"]:
        return {"planted": False, "message": f"the pool was generated for {pool['head_sha'][:12]}, the checkout is {head[:12]}",
                "reason": "commit mismatch", "candidate": None}
    if subprocess.run(["git", "diff", "--quiet"], cwd=workdir).returncode != 0:
        return {"planted": False, "message": "the checkout has local changes; refusing to plant", "reason": "dirty tree", "candidate": None}
    cand = candidate or random.Random(seed).choice(pool["candidates"])
    if form_check:  # False ONLY to re-measure a pre-round-4 pool; never in a real session
        check_diff(cand["diff"])  # raises PlantError: one changed line, no whitespace-only hunk, no spacing fingerprint
    r = subprocess.run(["git", "apply", "-"], cwd=workdir, input=cand["diff"], capture_output=True, text=True)
    if r.returncode != 0:
        return {"planted": False, "message": f"the pooled diff no longer applies: {r.stderr.strip()[:200]}", "reason": "apply failed",
                "candidate": None}
    return {"planted": True, "message": f"planted one validated bug in {cand['path']}", "candidate": cand}


def stranger_curve(stranger_results, cand_id: str, condition: str | None = None) -> dict | None:
    """The stranger's budget curve for THIS candidate, under the SAME visible file set: found within b actions, for
    b = 1..budget. A run under another condition is not comparable and is not used."""
    if stranger_results is None:
        return None
    data = _load(stranger_results)
    if data.get("condition") != condition:
        return None
    for r in data.get("results", []):
        if r.get("candidate_id") == cand_id:
            k, budget = r.get("actions_to_correct"), data.get("budget", DEFAULT_BUDGET)
            return {"found": bool(r.get("found")), "localisation_cost": r.get("localisation_cost", tools.UNBOUNDED),
                    "actions_to_correct": k, "budget": budget,
                    # the budget counts actions BEFORE the answer, so a find at action k is within budget b when k - 1 <= b
                    "found_within": {b: bool(k and k - 1 <= b) for b in range(1, budget + 1)},
                    "distinct_files_opened": r.get("distinct_files_opened"), "tier": data.get("tier"),
                    "condition": data.get("condition")}
    return None


class Session:
    """One participant, one planted bug. Use ``Session.start(...)``; it may come back refused (``.refused``)."""

    def __init__(self):
        self.refused, self.reason, self.cand, self.wt, self.tmp = None, None, None, None, None
        self.log: list[dict] = []
        self.opened: list[str] = []
        self.outcome: dict | None = None
        self.t0 = self.started_at = None
        self.budget = DEFAULT_BUDGET
        self.pool_head = None
        self.failing = None
        self.condition, self.hide = None, ()
        self.claims, self.targeting, self.how, self.result = None, None, None, None

    # ---- lifecycle -----------------------------------------------------------------------------------------------
    @classmethod
    def start(cls, repo: Path, pool, *, seed: int | None = None, budget: int = DEFAULT_BUDGET, python: str | None = None,
              candidate: dict | None = None, form_check: bool = True, condition: str | None = None,
              claims=None, allow_uncited: bool = False) -> "Session":
        """``condition``: the visible file set, "code" (docs/ hidden) or "docs" (nothing hidden); None = nothing hidden.
        ``claims``: the Phase 4 claim-verdict record (path or dict). The bug is then planted in a module the résumé claims,
        strongest claim first (bugpool.claims); when no candidate lands in one, nothing is planted and the record says
        so, unless ``allow_uncited``."""
        s = cls()
        # absolute: the tests run with the worktree as their working directory, where a relative path means nothing
        s.budget, s.python = budget, (str(Path(python).absolute()) if python and ("/" in python or Path(python).exists()) else python)
        s.condition, s.hide = condition, tools.hidden_for(condition)
        pool = _load(pool)
        s.pool_head = pool.get("head_sha")
        if pool.get("status") != "ok" or not pool.get("candidates"):
            s.refused, s.reason = INSUFFICIENT, pool.get("reason")  # step 1: nothing is planted
            return s
        if claims is not None:
            s.claims = claim_mod.load(claims)
            pool = claim_mod.annotate(pool, s.claims, str(claims) if isinstance(claims, (str, Path)) else None)
            s.targeting = pool["claim_targeting"]
            if candidate is None:
                candidate, s.how = claim_mod.choose(pool["candidates"], seed, allow_uncited)
                if candidate is None:  # a result, not a failure: never planted silently elsewhere
                    s.refused, s.reason, s.result = claim_mod.NO_CLAIMED_MODULE, "claim targeting", "no_claimed_module"
                    return s
            else:
                candidate = next((c for c in pool["candidates"] if candidate_id(c) == candidate_id(candidate)), candidate)
                s.how = "given"
        s.tmp = Path(tempfile.mkdtemp(prefix="bugpool-session-"))
        s.wt = s.tmp / "wt"
        subprocess.run(["git", "worktree", "add", "--detach", str(s.wt), pool["head_sha"]], cwd=repo, check=True, capture_output=True)
        s.repo = Path(repo)
        try:  # fail loudly, but never leave a worktree behind
            res = plant(pool, s.wt, seed=seed, candidate=candidate, form_check=form_check)
            if not res["planted"]:
                s.refused, s.reason = res["message"], res.get("reason")
                s._destroy()
                return s
            s.cand = res["candidate"]
            s.span = tools.statement_span((s.wt / s.cand["path"]).read_text(), s.cand["line"])  # scoring granularity
            killing = s.cand["killing_test"]
            s.failing = {"test": killing, "output": tools.test_run(s.wt, killing, s.python, hide=s.hide)}
            s.view()  # the Shield guard runs before the participant sees anything
        except BaseException:
            s._destroy()
            raise
        s.t0, s.started_at = time.monotonic(), datetime.now(timezone.utc).isoformat(timespec="seconds")
        return s

    def withheld(self) -> list[str]:
        """The résumé's claim texts: never shown during the hunt (the Shield rule)."""
        return [r.get("claim_text") or "" for r in (self.claims or {}).get("rows", [])]

    def view(self) -> dict:
        """Everything the participant is shown at the start: the repo and the failing test. Nothing else; never a claim."""
        v = {"files": tools.tree(self.wt, self.hide), "failing_test": self.failing["test"],
             "failing_output": self.failing["output"], "budget": self.budget}
        leaked = leaked_claims(json.dumps(v, ensure_ascii=False), self.withheld(), self.wt)
        if leaked:
            raise ShieldError(f"{len(leaked)} claim text(s) would reach the participant's view")
        return v

    # ---- the four actions ----------------------------------------------------------------------------------------
    def _act(self, kind: str, detail: dict) -> None:
        assert kind in tools.ACTIONS
        self.log.append({"t": round(time.monotonic() - self.t0, 2), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "type": kind, **detail})

    def _open(self) -> bool:
        return self.outcome is None

    def _budget_left(self) -> bool:
        used = sum(1 for e in self.log if e["type"] != "ANSWER")
        if used >= self.budget:
            self._finish(submitted=None)
            return False
        return True

    def file_open(self, path: str, start: int | None = None) -> str:
        if not self._open() or not self._budget_left():
            return "the session has ended"
        text, rel = tools.file_open(self.wt, path, start, hide=self.hide)
        self._act("FILE_OPEN", {"path": rel or path, "start": start or 1, "ok": rel is not None})
        if rel and rel not in self.opened:
            self.opened.append(rel)
        return text

    def search(self, pattern: str, path: str | None = None) -> str:
        if not self._open() or not self._budget_left():
            return "the session has ended"
        out = tools.search(self.wt, pattern, path, hide=self.hide)
        hit = sorted({ln.split(":", 1)[0] for ln in out.splitlines() if ":" in ln and not ln.startswith(("error", "..."))})
        self._act("SEARCH", {"pattern": pattern, "path": path, "files_hit": hit})
        return out

    def test_run(self, selector: str | None = None) -> str:
        if not self._open() or not self._budget_left():
            return "the session has ended"
        self._act("TEST_RUN", {"selector": selector or ""})
        return tools.test_run(self.wt, selector, self.python, hide=self.hide)

    def answer(self, path: str, line: int) -> dict:
        if not self._open():
            return self.outcome
        self._act("ANSWER", {"path": path, "line": line})
        return self._finish(submitted={"path": path, "line": line})

    def _finish(self, submitted) -> dict:
        actions = len(self.log)
        found = bool(submitted and tools.correct(self.cand, submitted["path"], submitted["line"], self.span))
        exact = bool(submitted and tools.correct(self.cand, submitted["path"], submitted["line"]))
        self.outcome = {"status": ("found" if found else "answered_elsewhere") if submitted else "not_found_within_budget",
                        "submitted": submitted, "correct": found, "correct_exact_line": exact,
                        "granularity": "statement", "statement_span": list(self.span),
                        "localisation_cost": tools.localisation_cost(self.log, self.cand["path"], found),
                        "actions_used": actions, "actions_to_correct": actions if found else None,
                        # recorded, not compared:
                        "distinct_files_opened": len(self.opened), "files_opened": list(self.opened),
                        "elapsed_seconds": round(time.monotonic() - self.t0, 1)}
        return self.outcome

    # ---- end -----------------------------------------------------------------------------------------------------
    def end(self, out: Path | None = None, stranger_results=None, registry: Path | None = None, burn: bool = True) -> dict:
        """``registry``: where the planted line is burned; default <repo>/.bugpool/burned.json. Pass a path OUTSIDE a
        repository that must not be modified. ``burn=False`` ONLY for the stranger: a line shown to a model is not a
        line a participant has seen, and the stranger must not write anything into the target repository."""
        """Destroy the worktree, BURN the planted line (unseen discipline), and return (and optionally write) the record."""
        if self.refused:
            record = {"refused": True, "message": self.refused, "reason": self.reason, "planted": False}
            if self.result:
                record.update(result=self.result, claim_targeting=self.targeting,
                              note="a result, not a failure: the pool has no validated bug in a module this résumé claims, "
                                   "and none was planted elsewhere")
        else:
            if self.outcome is None:
                self._finish(submitted=None)
            cid = candidate_id(self.cand)
            stranger = stranger_curve(stranger_results, cid, self.condition)
            record = {
                "refused": False, "pool_head_sha": self.pool_head, "started_at": self.started_at,
                "condition": self.condition, "hidden": list(self.hide),
                # THE headline: localisation cost on the same candidate, both sides with the same four actions
                "headline": {"localisation_cost": {"participant": self.outcome["localisation_cost"],
                                                   "stranger": stranger["localisation_cost"] if stranger else None},
                             "found": {"participant": self.outcome["correct"], "stranger": stranger["found"] if stranger else None},
                             "definition": "actions between first opening the correct file and answering it correctly; "
                                           "'unbounded' when the bug was never localised within the budget"},
                "candidate": {"id": cid, "module": self.cand["path"], "line": self.cand["line"], "operator": self.cand["operator"],
                              "family": self.cand.get("family"), "killing_test": self.cand["killing_test"],
                              "statement_span": list(self.span)},
                # the claim it targets: written only now that the session has ended (never shown during the hunt)
                "target": self._target(),
                # reviewer-facing only, and written only now that the session has ended
                "planted_diff": self.cand["diff"],
                "budget": self.budget, "action_types": list(tools.ACTIONS), "actions": self.log, "outcome": self.outcome,
                "comparison": {"participant_actions_to_find": self.outcome["actions_to_correct"], "stranger": stranger,
                               "note": "same candidate, same four actions; no score, no rank, no hire signal; elapsed time "
                                       "is recorded, never compared"},
            }
            if burn:
                reg = burned.Registry(registry or Path(self.repo) / burned.DEFAULT)
                reg.add(self.cand["path"], self.cand["line"], self.cand.get("original_line"), cid)
                reg.sources.append(f"session {self.started_at}: 1 planted line")
                reg.save()
            record["burned"] = burn
            self._destroy()
        if out:
            Path(out).write_text(json.dumps(record, indent=1))
        return record

    def _target(self) -> dict | None:
        """The résumé said X; the bug was planted in X; here is what happened."""
        if self.claims is None:
            return None
        t = self.cand.get("claim_target") or {}
        o = self.outcome
        happened = {"found": o["correct"], "localisation_cost": o["localisation_cost"], "status": o["status"]}
        if not t.get("cited"):
            return {"cited": False, "how_chosen": self.how, "message": claim_mod.NO_CLAIMED_MODULE,
                    "planted_module": self.cand["path"], "planted_line": self.cand["line"], "what_happened": happened,
                    "claim_targeting": self.targeting}
        best = next(c for c in t["claims"] if c["claim_id"] == t["claim_id"])
        return {"cited": True, "how_chosen": self.how, "claim_id": t["claim_id"],
                "claim_text": claim_mod.claim_text(self.claims, t["claim_id"]), "claim_kind": t["claim_kind"],
                "verdict": t["verdict"], "cited_module": self.cand["path"], "cited_lines": best["cited_lines"],
                "planted_module": self.cand["path"], "planted_line": self.cand["line"], "inside_cited_lines": t["span_cited"],
                "other_claims_citing_the_module": [c["claim_id"] for c in t["claims"] if c["claim_id"] != t["claim_id"]],
                "what_happened": happened, "claim_targeting": self.targeting}

    def _destroy(self) -> None:
        if self.wt is not None:
            subprocess.run(["git", "worktree", "remove", "--force", str(self.wt)], cwd=self.repo, capture_output=True)
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.wt = None
