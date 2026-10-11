"""THROWAWAY (Phase 5, Q3' parity harness; measurement, not the final evaluation).

The AI Stranger as a TOOL-USING AGENT with exactly a human's affordances: list files, read a file, run the test suite
(or any part of it), read the failure output. Fixed budget of tool calls. Super only, through the existing router.

    python q3b_agent.py POOL_JSON N OUT_JSON [BUDGET=15]

For each of the first N pool candidates: a throwaway git worktree at the pool's commit, the bug planted with
bugpool.session.plant, the agent run, the worktree discarded. The real working tree is never touched. The agent can
never read .env or any dotfile, nor anything outside the worktree.
"""
from dotenv import load_dotenv

load_dotenv()  # entry point only (model router credentials)

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import llm  # noqa: E402
from bugpool.session import plant  # noqa: E402

TIER, TASK = "super", "stranger_agent"
READ_LINES, OUT_CHARS, KEEP_CHARS = 250, 6000, 4000
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["thought", "action", "path", "start", "selector", "line"],
          "properties": {"thought": {"type": "string"},
                         "action": {"type": "string", "enum": ["list_files", "read_file", "run_tests", "answer"]},
                         "path": {"type": ["string", "null"]}, "start": {"type": ["integer", "null"]},
                         "selector": {"type": ["string", "null"]}, "line": {"type": ["integer", "null"]}}}
BRIEF = ("You are debugging a Python repository you have never seen. Someone changed ONE line of its source code, and "
         "the test suite now fails. Find that line. Tools (one per turn, answer with exactly one JSON action):\n"
         "  list_files  path = a directory relative to the repo root ('' for the root)\n"
         "  read_file   path = a file; start = first line to show (shows up to {n} numbered lines)\n"
         "  run_tests   selector = a pytest selector ('' runs the whole suite; a file or file::test runs part of it)\n"
         "  answer      path + line = the file and line number you believe was changed (ends the task)\n"
         "You have at most {b} tool calls before you must answer. Unused fields are null.")


def safe(wt: Path, rel: str | None) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    p = (wt / rel).resolve()
    if not p.is_relative_to(wt.resolve()) or any(part.startswith(".") for part in Path(rel).parts):
        return None  # outside the repo, or a dotfile / dot-directory (.env, .git ...)
    return p


def tool(wt: Path, act: dict) -> tuple[str, str | None]:
    """(observation, path opened by read_file or None)."""
    a = act.get("action")
    if a == "list_files":
        p = safe(wt, act.get("path"))
        if p is None or not p.is_dir():
            return "error: not a directory in the repository", None
        names = sorted(f"{c.name}/" if c.is_dir() else c.name for c in p.iterdir() if not c.name.startswith(".") and c.name != "__pycache__")
        return "\n".join(names)[:OUT_CHARS], None
    if a == "read_file":
        p = safe(wt, act.get("path"))
        if p is None or not p.is_file() or p.name.startswith(".env"):
            return "error: not a readable file in the repository", None
        try:
            lines = p.read_text().splitlines()
        except UnicodeDecodeError:
            return "error: binary file", None
        s = max(1, act.get("start") or 1)
        chunk = lines[s - 1: s - 1 + READ_LINES]
        rel = p.relative_to(wt.resolve()).as_posix()
        return (f"{rel} lines {s}-{s + len(chunk) - 1} of {len(lines)}\n" + "\n".join(f"{s + i:>5}| {ln}" for i, ln in enumerate(chunk))), rel
    if a == "run_tests":
        sel = (act.get("selector") or "").strip()
        if sel.startswith(("-", "/")) or ".." in sel:
            return "error: give a pytest selector (a test path), not options", None
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *([sel] if sel else [])],
                               cwd=wt, capture_output=True, text=True, timeout=900)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            out = "error: the test run timed out after 900 s"
        return out[-OUT_CHARS:], None
    return "error: unknown action", None


def run_agent(wt: Path, cand: dict, budget: int) -> dict:
    hist, opened, calls, cost, answer, refusals, turns = [], [], 0, 0.0, None, 0, 0
    t0 = time.monotonic()
    while answer is None:
        forced = calls >= budget
        transcript = "\n\n".join(f"#{i + 1} ACTION {json.dumps(h['act'])}\nOBSERVATION:\n{h['obs']}" for i, h in enumerate(hist))
        prompt = (BRIEF.format(n=READ_LINES, b=budget) + f"\n\nTool calls used: {calls} of {budget}."
                  + ("\nBUDGET EXHAUSTED: you must now use the answer action." if forced else "")
                  + (f"\n\nSO FAR:\n{transcript}" if hist else ""))
        try:
            r = llm.call(TASK, prompt, json_schema=SCHEMA, tier=TIER, thinking=False, temperature=0, max_tokens=800,
                         run_tag="phase5-q3b")
            act, c = r.get("data") or {}, r.get("cost_usd") or 0.0
        except llm.LLMJSONError:
            act, c = {"action": "unparseable"}, 0.0
        except llm.LLMRefusalError:
            # measured: the router's lexical refusal detector fired on the agent's own "thought" text. A false
            # positive for this use; recorded as a turn with no tool call, not a crash
            act, c = {"action": "refused_by_detector"}, 0.0
            refusals += 1
        cost += c
        turns += 1
        if act.get("action") in ("unparseable", "refused_by_detector") and turns < 3 * budget:
            continue  # no tool call made; ask again (bounded)
        if act.get("action") == "answer" or forced:
            answer = {"path": act.get("path"), "line": act.get("line"), "forced": forced and act.get("action") != "answer"}
            break
        obs, rel = tool(wt, act)
        calls += 1
        if rel and rel not in opened:
            opened.append(rel)
        hist.append({"act": {k: v for k, v in act.items() if k != "thought" and v is not None}, "obs": obs[:KEEP_CHARS]})
    found = answer["path"] == cand["path"] and isinstance(answer["line"], int) and cand["line"] <= answer["line"] <= cand["end_line"]
    return {"found": found, "answer": answer, "files_opened": opened, "distinct_files_opened": len(opened), "tool_calls": calls,
            "cost_usd": round(cost, 5), "wall_clock_s": round(time.monotonic() - t0, 1),
            "actions": [h["act"].get("action") for h in hist], "model_turns": turns, "detector_refusals": refusals}


def main():
    pool = json.loads(Path(sys.argv[1]).read_text())
    n, out = int(sys.argv[2]), Path(sys.argv[3])
    budget = int(sys.argv[4]) if len(sys.argv) > 4 else 15
    results = []
    for k, cand in enumerate(pool["candidates"][:n], 1):
        with tempfile.TemporaryDirectory(prefix="q3b-") as tmp:
            wt = Path(tmp) / "wt"
            subprocess.run(["git", "worktree", "add", "--detach", str(wt), pool["head_sha"]], cwd=ROOT, check=True, capture_output=True)
            try:
                one = dict(pool, candidates=[cand])
                res = plant(one, wt)
                assert res["planted"], res
                r = run_agent(wt, cand, budget)
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
        r.update(path=cand["path"], line=cand["line"], family=cand["family"], killing_test=cand["killing_test"])
        results.append(r)
        out.write_text(json.dumps({"budget": budget, "tier": TIER, "results": results}, indent=1))  # after every candidate
        print(f"[{k}/{n}] found={r['found']} files={r['distinct_files_opened']} calls={r['tool_calls']} ${r['cost_usd']} "
              f"{r['wall_clock_s']}s  {cand['path']}:{cand['line']}  answered {r['answer']}", flush=True)
    out.write_text(json.dumps({"budget": budget, "tier": TIER, "results": results}, indent=1))
    clean = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode == 0
    print("real tree clean:", clean)


if __name__ == "__main__":
    main()
