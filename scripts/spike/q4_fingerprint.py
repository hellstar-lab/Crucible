"""THROWAWAY (Phase 5 round 4, B.4). Is the UNMUTATED form of a pooled line distinguishable from the other lines of its
file by a cheap signal? For each pooled line: the share of its file's code lines that carry a mutation-eligible token
(what a hunter who knows the operator whitelist could narrow to), and the line's percentile within its file for length,
indentation depth and enclosing-function complexity (lizard CCN, as the repo's own tooling computes it)."""
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

import lizard

ROOT = Path(__file__).resolve().parents[2]
ELIGIBLE = re.compile(r"(==|!=|<=|>=|<|>|\bis\b|\bnot\b|\band\b|\bor\b|\bTrue\b|\bFalse\b|\bbreak\b|\bcontinue\b|(?<![\w.])\d+(?![\w.]))")


def pct(values, v):
    return round(100 * sum(1 for x in values if x < v) / len(values))


rows = []
for pool_path in sys.argv[1:]:
    pool = json.loads(Path(pool_path).read_text())
    for c in pool["candidates"]:
        text = subprocess.run(["git", "show", f"{pool['head_sha']}:{c['path']}"], cwd=ROOT, capture_output=True, text=True).stdout
        lines = text.splitlines()
        code = [(i, ln) for i, ln in enumerate(lines, 1) if ln.strip() and not ln.strip().startswith(("#", '"', "'"))]
        elig = [(i, ln) for i, ln in code if ELIGIBLE.search(ln.split("#")[0])]
        fns = lizard.analyze_file.analyze_source_code(c["path"], text).function_list
        ccn_of = {i: f.cyclomatic_complexity for f in fns for i in range(f.start_line, f.end_line + 1)}
        target = lines[c["line"] - 1]
        rows.append({"line": f"{c['path']}:{c['line']}", "eligible_share": round(len(elig) / len(code), 2),
                     "len_pct": pct([len(ln.strip()) for _i, ln in elig], len(target.strip())),
                     "indent_pct": pct([len(ln) - len(ln.lstrip()) for _i, ln in elig], len(target) - len(target.lstrip())),
                     "ccn_pct": pct([ccn_of.get(i, 0) for i, _ in elig], ccn_of.get(c["line"], 0))})
for r in rows:
    print(r)
print("median eligible share:", statistics.median(r["eligible_share"] for r in rows))
for k in ("len_pct", "indent_pct", "ccn_pct"):
    v = [r[k] for r in rows]
    print(f"{k}: median {statistics.median(v)}  range {min(v)}-{max(v)}")
