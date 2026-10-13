"""THROWAWAY (Phase 5, Part A: stranger parity). The AI stranger with EXACTLY the participant's view and actions,
through the shared bugpool.tools: it starts with the repository's file tree and the failing test's name and output,
and acts with FILE_OPEN, SEARCH, TEST_RUN, ANSWER. Budget ceiling 40 actions before the answer. Super only.

    python q3c_agent.py POOL_JSON N OUT_JSON [BUDGET=40] [--repo PATH] [--python TEST_PYTHON] [--condition code|docs]
                        [--sealed]

--condition sets the visible file set through the SAME Session option the participant gets (bugpool.tools.CONDITIONS):
"code" hides docs/, "docs" shows it. --sealed prints no candidate location (module, line, submitted answer): only the
candidate's ordinal and the outcome, so a pool that a participant will hunt stays unseen.

Records, per candidate, the action index at which it named the correct line (actions_to_correct, counting the ANSWER)
and writes the budget curve found(b) for b = 1..BUDGET. found(b) counts a candidate when actions_to_correct <= b; a run
capped at a smaller budget would have been forced to answer earlier, so the curve is an upper envelope of what a
smaller budget would score.
"""
from dotenv import load_dotenv

load_dotenv()  # entry point only (model router credentials)

import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import llm  # noqa: E402
from bugpool import tools  # noqa: E402
from bugpool.fingerprint import PlantError, check_diff  # noqa: E402
from bugpool.session import Session, candidate_id  # noqa: E402

TIER, TASK, KEEP_CHARS = "super", "stranger_agent", 4000
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["thought", "action", "path", "start", "pattern", "selector", "line"],
          "properties": {"thought": {"type": "string"},
                         "action": {"type": "string", "enum": ["FILE_OPEN", "SEARCH", "TEST_RUN", "ANSWER"]},
                         "path": {"type": ["string", "null"]}, "start": {"type": ["integer", "null"]},
                         "pattern": {"type": ["string", "null"]}, "selector": {"type": ["string", "null"]},
                         "line": {"type": ["integer", "null"]}}}
BRIEF = ("You are debugging a Python repository you have never seen. Someone changed ONE line of its source code and a "
         "test now fails. Find that line. One action per turn, as exactly one JSON object:\n"
         "  FILE_OPEN  path (+ start = first line; shows up to {n} numbered lines)\n"
         "  SEARCH     pattern = a regex searched across the repository's files (path = optional file or directory)\n"
         "  TEST_RUN   selector = a pytest selector ('' = the whole suite)\n"
         "  ANSWER     path + line = the file and line you believe was changed (ends the task)\n"
         "You have at most {b} actions before you must answer. Unused fields are null.\n\n"
         "THE FAILING TEST: {test}\nITS OUTPUT:\n{output}\n\nTHE REPOSITORY'S FILES:\n{files}")


def run(sess: Session, budget: int) -> dict:
    v = sess.view()
    brief = BRIEF.format(n=tools.READ_LINES, b=budget, test=v["failing_test"], output=v["failing_output"], files="\n".join(v["files"]))
    hist, cost, turns = [], 0.0, 0
    t0 = time.monotonic()
    while sess.outcome is None:
        used = sum(1 for e in sess.log if e["type"] != "ANSWER")
        forced = used >= budget
        transcript = "\n\n".join(f"#{i + 1} {json.dumps(h['act'])}\nRESULT:\n{h['obs']}" for i, h in enumerate(hist))
        prompt = (brief + f"\n\nActions used: {used} of {budget}." + ("\nBUDGET EXHAUSTED: answer now." if forced else "")
                  + (f"\n\nSO FAR:\n{transcript}" if hist else ""))
        try:
            r = llm.call(TASK, prompt, json_schema=SCHEMA, tier=TIER, thinking=False, temperature=0, max_tokens=800, run_tag="phase5-q3d")
            act, c = r.get("data") or {}, r.get("cost_usd") or 0.0
        except llm.LLMJSONError:
            act, c = {"action": "unparseable"}, 0.0
        cost, turns = cost + c, turns + 1
        a = act.get("action")
        if a == "unparseable" and turns < 3 * budget:
            continue
        if a == "ANSWER" or forced:
            sess.answer(act.get("path"), act.get("line") if isinstance(act.get("line"), int) else -1)
            break
        if a == "FILE_OPEN":
            obs = sess.file_open(act.get("path") or "", act.get("start"))
        elif a == "SEARCH":
            obs = sess.search(act.get("pattern") or "", act.get("path"))
        elif a == "TEST_RUN":
            obs = sess.test_run(act.get("selector"))
        else:
            obs = "error: unknown action"
        hist.append({"act": {k: v for k, v in act.items() if k != "thought" and v is not None}, "obs": obs[:KEEP_CHARS]})
    o = sess.outcome
    return {"found": o["correct"], "correct_exact_line": o["correct_exact_line"], "localisation_cost": o["localisation_cost"],
            "statement_span": o["statement_span"], "log": sess.log, "actions_to_correct": o["actions_to_correct"],
            "actions_used": o["actions_used"],
            "submitted": o["submitted"], "forced": o["status"] == "not_found_within_budget" or (o["submitted"] or {}).get("line") == -1,
            "distinct_files_opened": o["distinct_files_opened"], "files_opened": o["files_opened"], "cost_usd": round(cost, 5),
            "wall_clock_s": round(time.monotonic() - t0, 1), "action_types": [e["type"] for e in sess.log]}


def main():
    import argparse
    import hashlib
    import subprocess

    ap = argparse.ArgumentParser()
    ap.add_argument("pool", type=Path)
    ap.add_argument("n", type=int)
    ap.add_argument("out", type=Path)
    ap.add_argument("budget", type=int, nargs="?", default=40)
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--python", help="the interpreter that runs the repository's tests")
    ap.add_argument("--condition", choices=sorted(tools.CONDITIONS))
    ap.add_argument("--sealed", action="store_true")
    a = ap.parse_args()
    pool = json.loads(a.pool.read_text())
    n, out, budget, repo = a.n, a.out, a.budget, a.repo.resolve()

    def snapshot() -> str:  # the working tree as it is now (uncommitted edits included), to prove it is never changed
        diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo, capture_output=True).stdout
        st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True).stdout
        return hashlib.sha256(diff + st).hexdigest()

    before = snapshot()
    results = []
    for k, cand in enumerate(pool["candidates"][:n], 1):
        # measurement-only: round 3's pool predates the round-4 form check; three of its ten are fingerprinted and a real
        # session would refuse them. They are planted here only to keep the ten comparable, and marked.
        try:
            check_diff(cand["diff"])
            fingerprinted = False
        except PlantError:
            fingerprinted = True
        sess = Session.start(repo, pool, budget=budget, candidate=cand, form_check=False, python=a.python, condition=a.condition)
        assert not sess.refused, sess.refused
        try:
            r = run(sess, budget)
        finally:
            sess.end(burn=False)  # a model, not a participant; and nothing is written into the target repo
        r.update(candidate_id=candidate_id(cand), path=cand["path"], line=cand["line"], family=cand["family"],
                 killing_test=cand["killing_test"], fingerprinted=fingerprinted)
        results.append(r)
        # found within b = the correct answer came after at most b actions (the budget counts actions before the answer)
        curve = {b: sum(1 for x in results if x["actions_to_correct"] and x["actions_to_correct"] - 1 <= b) for b in range(1, budget + 1)}
        out.write_text(json.dumps({"budget": budget, "tier": TIER, "condition": a.condition, "hidden": list(tools.hidden_for(a.condition)),
                                   "results": results, "curve_found_within": curve}, indent=1))
        where = "" if a.sealed else f"  {cand['path']}:{cand['line']}  submitted {r['submitted']}"
        print(f"[{k}/{n}] found={r['found']} loc={r['localisation_cost']} at={r['actions_to_correct']} files={r['distinct_files_opened']} "
              f"actions={r['actions_used']} ${r['cost_usd']} {r['wall_clock_s']}s{where}", flush=True)
    print("repository unchanged by the run:", snapshot() == before)


if __name__ == "__main__":
    main()
