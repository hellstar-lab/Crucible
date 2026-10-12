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
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import INSUFFICIENT, tools

DEFAULT_BUDGET = 40  # actions before the answer, the stranger's ceiling


def candidate_id(c: dict) -> str:
    return f"{c['path']}:{c['line']}:{c['operator'].split('/')[-1]}:{hashlib.sha256(c['diff'].encode()).hexdigest()[:8]}"


def _load(pool):
    return json.loads(Path(pool).read_text()) if isinstance(pool, (str, Path)) else pool


def plant(pool, workdir: Path, *, seed: int | None = None, candidate: dict | None = None) -> dict:
    """{"planted": bool, "message": str, "candidate": dict | None}. ``workdir`` is a checkout at the pool's commit."""
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
    r = subprocess.run(["git", "apply", "-"], cwd=workdir, input=cand["diff"], capture_output=True, text=True)
    if r.returncode != 0:
        return {"planted": False, "message": f"the pooled diff no longer applies: {r.stderr.strip()[:200]}", "reason": "apply failed",
                "candidate": None}
    return {"planted": True, "message": f"planted one validated bug in {cand['path']}", "candidate": cand}


def stranger_curve(stranger_results, cand_id: str) -> dict | None:
    """The stranger's budget curve for THIS candidate: found within b actions, for b = 1..budget."""
    if stranger_results is None:
        return None
    data = _load(stranger_results)
    for r in data.get("results", []):
        if r.get("candidate_id") == cand_id:
            k, budget = r.get("actions_to_correct"), data.get("budget", DEFAULT_BUDGET)
            return {"budget": budget, "actions_to_correct": k, "found_within": {b: bool(k and k <= b) for b in range(1, budget + 1)},
                    "distinct_files_opened": r.get("distinct_files_opened"), "tier": data.get("tier")}
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

    # ---- lifecycle -----------------------------------------------------------------------------------------------
    @classmethod
    def start(cls, repo: Path, pool, *, seed: int | None = None, budget: int = DEFAULT_BUDGET, python: str | None = None,
              candidate: dict | None = None) -> "Session":
        s = cls()
        s.budget, s.python = budget, python
        pool = _load(pool)
        s.pool_head = pool.get("head_sha")
        if pool.get("status") != "ok" or not pool.get("candidates"):
            s.refused, s.reason = INSUFFICIENT, pool.get("reason")  # step 1: nothing is planted
            return s
        s.tmp = Path(tempfile.mkdtemp(prefix="bugpool-session-"))
        s.wt = s.tmp / "wt"
        subprocess.run(["git", "worktree", "add", "--detach", str(s.wt), pool["head_sha"]], cwd=repo, check=True, capture_output=True)
        s.repo = Path(repo)
        res = plant(pool, s.wt, seed=seed, candidate=candidate)
        if not res["planted"]:
            s.refused, s.reason = res["message"], res.get("reason")
            s._destroy()
            return s
        s.cand = res["candidate"]
        killing = s.cand["killing_test"]
        s.failing = {"test": killing, "output": tools.test_run(s.wt, killing, python)}
        s.t0, s.started_at = time.monotonic(), datetime.now(timezone.utc).isoformat(timespec="seconds")
        return s

    def view(self) -> dict:
        """Everything the participant is shown at the start: the repo and the failing test. Nothing else."""
        return {"files": tools.tree(self.wt), "failing_test": self.failing["test"], "failing_output": self.failing["output"],
                "budget": self.budget}

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
        text, rel = tools.file_open(self.wt, path, start)
        self._act("FILE_OPEN", {"path": rel or path, "start": start or 1, "ok": rel is not None})
        if rel and rel not in self.opened:
            self.opened.append(rel)
        return text

    def search(self, pattern: str, path: str | None = None) -> str:
        if not self._open() or not self._budget_left():
            return "the session has ended"
        self._act("SEARCH", {"pattern": pattern, "path": path})
        return tools.search(self.wt, pattern, path)

    def test_run(self, selector: str | None = None) -> str:
        if not self._open() or not self._budget_left():
            return "the session has ended"
        self._act("TEST_RUN", {"selector": selector or ""})
        return tools.test_run(self.wt, selector, self.python)

    def answer(self, path: str, line: int) -> dict:
        if not self._open():
            return self.outcome
        self._act("ANSWER", {"path": path, "line": line})
        return self._finish(submitted={"path": path, "line": line})

    def _finish(self, submitted) -> dict:
        actions = len(self.log)
        found = bool(submitted and tools.correct(self.cand, submitted["path"], submitted["line"]))
        self.outcome = {"status": ("found" if found else "answered_elsewhere") if submitted else "not_found_within_budget",
                        "submitted": submitted, "correct": found, "actions_used": actions,
                        "actions_to_correct": actions if found else None, "distinct_files_opened": len(self.opened),
                        "files_opened": list(self.opened), "elapsed_seconds": round(time.monotonic() - self.t0, 1)}
        return self.outcome

    # ---- end -----------------------------------------------------------------------------------------------------
    def end(self, out: Path | None = None, stranger_results=None) -> dict:
        """Destroy the worktree and return (and optionally write) the session record."""
        if self.refused:
            record = {"refused": True, "message": self.refused, "reason": self.reason, "planted": False}
        else:
            if self.outcome is None:
                self._finish(submitted=None)
            cid = candidate_id(self.cand)
            record = {
                "refused": False, "pool_head_sha": self.pool_head, "started_at": self.started_at,
                "candidate": {"id": cid, "operator": self.cand["operator"], "family": self.cand.get("family"),
                              "killing_test": self.cand["killing_test"]},
                "budget": self.budget, "action_types": list(tools.ACTIONS), "actions": self.log, "outcome": self.outcome,
                "comparison": {"participant_actions_to_find": self.outcome["actions_to_correct"],
                               "stranger": stranger_curve(stranger_results, cid),
                               "note": "actions-to-find on the same candidate, both sides with the same four actions; "
                                       "no score, no rank, no hire signal; elapsed time is recorded, never compared"},
            }
            self._destroy()
        if out:
            Path(out).write_text(json.dumps(record, indent=1))
        return record

    def _destroy(self) -> None:
        if self.wt is not None:
            subprocess.run(["git", "worktree", "remove", "--force", str(self.wt)], cwd=self.repo, capture_output=True)
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.wt = None
