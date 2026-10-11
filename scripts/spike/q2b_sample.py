"""THROWAWAY (Phase 5 spike, Q2'). Sample restricted candidates; apply one mutation (cosmic-ray apply), run ONLY its
covering tests (path::function keys, so parametrized ids never matter), revert (git checkout), verify clean, classify:
BROKEN, SURVIVOR, NOISY KILL (2+ failing test functions), EXCEPTION-AT-SITE, TELEGRAPHED, CLEAN.

    python q2b_sample.py RESTRICTED_JSON OUT_JSON N SEED
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
GENERIC = set("""self none true false return items item result results list dict text path line lines name names data value
values key keys test tests with from import else elif lambda len str int float get set append extend join split strip
lower upper format range enumerate sorted isinstance kind type count index start end first last left right page pages
out file files args kwargs config match find search""".split())
_FRAME = re.compile(r"^(\S+\.py):(\d+): (\w+(?:\.\w+)*)\s*$", re.M)


def clean() -> bool:
    return subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode == 0


def enclosing(path, line):
    tree = ast.parse((ROOT / path).read_text())
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= node.end_lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best else None


def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 4} - GENERIC


def telegraph_terms(path, start, end, func):
    src = (ROOT / path).read_text().splitlines()
    window = src[max(0, start - 3): end + 2]  # the mutated line(s) and two either side
    terms = set()
    for ln in window:
        for ident in re.findall(r"[A-Za-z_]\w+", ln):
            terms |= tokens(ident.replace("_", " ")) | ({ident.lower()} if len(ident) >= 4 and ident.lower() not in GENERIC else set())
        for lit in re.findall(r"[\"']([^\"']{4,})[\"']", ln):
            terms |= tokens(lit)
    if func:
        terms |= {func.lower().strip("_")} | tokens(func.replace("_", " "))
    return terms - GENERIC


def main():
    cands = json.loads(Path(sys.argv[1]).read_text())
    out, n, seed = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    sample = random.Random(seed).sample(cands, n)
    assert clean(), "working tree must be clean"
    results = []
    for k, m in enumerate(sample, 1):
        func = enclosing(m["path"], m["start"][0])
        r = subprocess.run([CR, "apply", m["path"], m["operator"], str(m["occurrence"])], cwd=ROOT, capture_output=True, text=True)
        diff = subprocess.run(["git", "diff", "-U2", "--", m["path"]], cwd=ROOT, capture_output=True, text=True).stdout
        rec = {**m, "func": func, "diff": diff, "apply_rc": r.returncode}
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
            failed, coll = {}, False
            if xml.exists():
                for tc in ET.parse(xml).getroot().iter("testcase"):
                    bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                    if bad is None:
                        continue
                    if not tc.get("name"):
                        coll = True
                        continue
                    fn = f"{tc.get('classname', '').replace('.', '/')}.py::{re.sub(r'[[].*$', '', tc.get('name'))}"
                    text = bad.text or ""
                    frames = _FRAME.findall(text)
                    failed.setdefault(fn, {"test": fn, "message": (bad.get("message") or "")[:500], "innermost": frames[-1] if frames else None})
        subprocess.run(["git", "checkout", "--", m["path"]], cwd=ROOT, check=True)
        assert clean(), f"revert failed: {m['path']}"
        failed = list(failed.values())
        terms = telegraph_terms(m["path"], m["start"][0], m["end"][0], func)
        if hung or r.returncode != 0 or rc in (2, 3, 4) or coll or not diff:
            cls = "BROKEN"
        elif not failed:
            cls = "SURVIVOR"
        elif len(failed) > 1:
            cls = "NOISY KILL"
        else:
            f = failed[0]
            inner = f["innermost"]
            at_site = bool(inner and inner[0] == m["path"] and m["start"][0] <= int(inner[1]) <= m["end"][0]
                           and inner[2].split(".")[-1] not in ("AssertionError",))
            name_toks = tokens(f["test"].split("::")[-1].replace("_", " "))
            msg_toks = tokens(f["message"])
            hits = sorted(t for t in terms if t in name_toks or t in msg_toks or (len(t) >= 6 and t in f["message"].lower()))
            f["telegraph_hits"] = hits
            cls = "EXCEPTION-AT-SITE" if at_site else ("TELEGRAPHED" if hits else "CLEAN")
        rec.update(cls=cls, rc=rc, failed=failed, covering=len(m["covering_tests"]), telegraph_terms=sorted(terms))
        results.append(rec)
        print(f"[{k}/{n}] {cls:<18} {m['family']:<18} {m['path']}:{m['start'][0]} tests={len(m['covering_tests'])} "
              f"failed={len(failed)} {rec['seconds']}s", flush=True)
    out.write_text(json.dumps(results, indent=1))
    assert clean()
    print("tree clean after all mutations:", clean())


if __name__ == "__main__":
    main()
