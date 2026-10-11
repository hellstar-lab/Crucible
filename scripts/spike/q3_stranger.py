"""THROWAWAY (Phase 5 spike). The AI-stranger baseline on clean, non-telegraphing kills.

    python q3_stranger.py Q2_RESULTS_JSON JOB_IDS(comma) OUT_JSON

Condition A (file-level): the mutated file with line numbers + failing test name + assertion message -> the line.
Condition B (repo-level): failing test name + assertion + the repo file TREE; the model asks for up to 5 files, gets their
(mutated) contents, then names path + line. Up to 3 attempts per condition; a miss is fed back ("<path>:<line> is not the
bug"). Tiers nano and super through the existing router (task stranger_agent). Each mutation is applied only while its
file is read, then reverted, and the tree is verified clean.
"""
from dotenv import load_dotenv

load_dotenv()  # entry point only

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import llm  # noqa: E402

CR = str(Path(sys.executable).with_name("cosmic-ray"))
TIERS = ("nano", "super")
ATTEMPTS = 3
TASK = "stranger_agent"


def clean() -> bool:
    return subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode == 0


def mutated_text(m) -> str:
    assert clean()
    subprocess.run([CR, "apply", m["path"], m["operator"], str(m["occurrence"])], cwd=ROOT, check=True, capture_output=True)
    text = (ROOT / m["path"]).read_text()
    subprocess.run(["git", "checkout", "--", m["path"]], cwd=ROOT, check=True)
    assert clean()
    return text


def numbered(text: str) -> str:
    return "\n".join(f"{i:>4}| {ln}" for i, ln in enumerate(text.splitlines(), 1))


def tree() -> list[str]:
    files = subprocess.run(["git", "ls-files", "backend", "tests", "scripts"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    return [f for f in files if f.endswith(".py") and "/spike/" not in f]


def failure(m) -> str:
    f = m["failed"][0]
    return f"Failing test: {f['test']}\nAssertion message: {f['message']}"


LINE = {"type": "object", "additionalProperties": False, "required": ["line", "reason"],
        "properties": {"line": {"type": "integer"}, "reason": {"type": "string"}}}
PICK = {"type": "object", "additionalProperties": False, "required": ["files"],
        "properties": {"files": {"type": "array", "items": {"type": "string"}}}}
PATH_LINE = {"type": "object", "additionalProperties": False, "required": ["path", "line", "reason"],
             "properties": {"path": {"type": "string"}, "line": {"type": "integer"}, "reason": {"type": "string"}}}
PRE = ("A single-line bug was introduced into this Python repository and one test now fails. Find the line. "
       "Answer only with the JSON object.")


def hit(m, path, line) -> bool:
    return path == m["path"] and m["start"][0] <= int(line) <= m["end"][0]


UNPARSEABLE = {"n": 0}


def call(prompt, schema, tier):
    """An unparseable reply is a MISSED attempt (counted, not hidden), not a crash."""
    t0 = time.monotonic()
    try:
        r = llm.call(TASK, prompt, json_schema=schema, tier=tier, thinking=False, temperature=0, max_tokens=1200,
                     run_tag="phase5-spike")
    except llm.LLMJSONError:
        UNPARSEABLE["n"] += 1
        return {"unparseable": True}, time.monotonic() - t0, 0.0
    return r.get("data") or {}, time.monotonic() - t0, r.get("cost_usd") or 0.0


def condition_a(m, text, tier):
    misses, rec = [], []
    for k in range(1, ATTEMPTS + 1):
        prompt = (f"{PRE}\n\n{failure(m)}\n\nFile {m['path']}:\n{numbered(text)}"
                  + (f"\n\nAlready ruled out (not the bug): lines {', '.join(map(str, misses))}." if misses else ""))
        d, secs, cost = call(prompt, LINE, tier)
        ok = hit(m, m["path"], d.get("line", -1))
        rec.append({"attempt": k, "line": d.get("line"), "hit": ok, "seconds": round(secs, 2), "cost_usd": cost})
        if ok:
            break
        misses.append(d.get("line"))
    return rec


def condition_b(m, text, tier, files):
    misses, rec = [], []
    for k in range(1, ATTEMPTS + 1):
        ruled = f"\n\nAlready ruled out (not the bug): {', '.join(misses)}." if misses else ""
        d1, s1, c1 = call(f"{PRE}\n\n{failure(m)}\n\nRepository files:\n" + "\n".join(files) +
                          f"\n\nList up to 5 file paths you want to read, most likely first.{ruled}", PICK, tier)
        picked = [f for f in (d1.get("files") or []) if f in files][:5]
        body = "\n\n".join(f"File {f}:\n{numbered(text if f == m['path'] else (ROOT / f).read_text())}" for f in picked)
        d2, s2, c2 = call(f"{PRE}\n\n{failure(m)}\n\n{body}\n\nName the file and line.{ruled}", PATH_LINE, tier)
        ok = hit(m, d2.get("path"), d2.get("line", -1))
        rec.append({"attempt": k, "picked": picked, "picked_target": m["path"] in picked, "path": d2.get("path"),
                    "line": d2.get("line"), "hit": ok, "seconds": round(s1 + s2, 2), "cost_usd": c1 + c2})
        if ok:
            break
        misses.append(f"{d2.get('path')}:{d2.get('line')}")
    return rec


def main():
    results = {r["job_id"]: r for r in json.loads(Path(sys.argv[1]).read_text())}
    ids, out = sys.argv[2].split(","), Path(sys.argv[3])
    files = tree()
    report = []
    for jid in ids:
        m = results[jid]
        text = mutated_text(m)
        for tier in TIERS:
            report.append({"job_id": jid, "path": m["path"], "line": m["start"][0], "tier": tier, "condition": "A",
                           "attempts": condition_a(m, text, tier)})
            report.append({"job_id": jid, "path": m["path"], "line": m["start"][0], "tier": tier, "condition": "B",
                           "attempts": condition_b(m, text, tier, files)})
            print(jid, tier, [(r["condition"], [a["hit"] for a in r["attempts"]]) for r in report[-2:]], flush=True)
    out.write_text(json.dumps({"report": report, "unparseable_replies": UNPARSEABLE["n"]}, indent=1))
    assert clean()
    print("tree clean:", clean())


if __name__ == "__main__":
    main()
