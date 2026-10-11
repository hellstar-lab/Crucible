"""THROWAWAY (Phase 5 spike). Random sample of filtered candidates: apply one mutation (cosmic-ray apply), run ONLY its
covering tests, revert (git checkout), verify the tree is clean, classify.

    python q2_sample.py SURVIVORS_JSON OUT_JSON [N=25] [SEED=5]
CLEAN KILL = exactly one test fails; NOISY KILL = 2+; SURVIVOR = none; BROKEN = collection/import/syntax error or hang.
"""
import ast
import json
import random
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
CR = str(Path(PY).with_name("cosmic-ray"))
TIMEOUT_S = 600

survivors = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
n = int(sys.argv[3]) if len(sys.argv) > 3 else 25
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 5
sample = random.Random(seed).sample(survivors, n)


def clean() -> bool:
    return subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode == 0


def enclosing(path: str, line: int) -> str | None:
    tree = ast.parse((ROOT / path).read_text())
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.lineno <= line <= node.end_lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best else None


assert clean(), "working tree must be clean before mutating"
results = []
for k, m in enumerate(sample, 1):
    src_line = (ROOT / m["path"]).read_text().splitlines()[m["start"][0] - 1]
    func = enclosing(m["path"], m["start"][0])
    r = subprocess.run([CR, "apply", m["path"], m["operator"], str(m["occurrence"])], cwd=ROOT, capture_output=True, text=True)
    diff = subprocess.run(["git", "diff", "-U1", "--", m["path"]], cwd=ROOT, capture_output=True, text=True).stdout
    rec = {**m, "func": func, "line_text": src_line.strip(), "diff": diff, "apply_rc": r.returncode}
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "j.xml"
        t0 = time.monotonic()
        try:
            p = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *m["covering_tests"]],
                               cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S)
            rc, hung = p.returncode, False
        except subprocess.TimeoutExpired:
            rc, hung = None, True
        rec["seconds"] = round(time.monotonic() - t0, 1)
        failed, collection_error = [], False
        if xml.exists():
            for tc in ET.parse(xml).getroot().iter("testcase"):
                bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                if bad is None:
                    continue
                nid = f"{tc.get('classname', '').replace('.', '/')}.py::{tc.get('name')}"
                if not tc.get("name"):
                    collection_error = True
                failed.append({"test": nid, "message": (bad.get("message") or "")[:400]})
    subprocess.run(["git", "checkout", "--", m["path"]], cwd=ROOT, check=True)
    assert clean(), f"revert failed for {m['path']}"
    if hung or r.returncode != 0 or rc in (2, 3, 4) or collection_error or not diff:
        cls = "BROKEN"
    elif not failed:
        cls = "SURVIVOR"
    else:
        cls = "CLEAN KILL" if len(failed) == 1 else "NOISY KILL"
    # telegraph (mechanical): the failing test's name or message names the enclosing function or a >=4-char identifier
    # from the mutated line
    idents = {w for w in re.findall(r"[A-Za-z_]\w{3,}", src_line)} | ({func} if func else set())
    idents -= {"self", "return", "None", "True", "False", "elif", "else", "with", "lambda", "assert", "raise", "import"}
    tele = []
    for f in failed:
        hay = (f["test"].split("::")[-1] + " " + f["message"]).lower()
        hits = sorted(i for i in idents if i.lower() in hay or i.lower().strip("_") in hay)
        tele.append(hits)
    rec.update(cls=cls, rc=rc, hung=hung, failed=failed, covering=len(m["covering_tests"]), telegraph_hits=tele)
    results.append(rec)
    print(f"[{k}/{n}] {cls:<10} {m['operator']:<45} {m['path']}:{m['start'][0]} tests={len(m['covering_tests'])} "
          f"failed={len(failed)} {rec['seconds']}s", flush=True)
out.write_text(json.dumps(results, indent=1))
assert clean()
print("tree clean after all mutations:", clean())
