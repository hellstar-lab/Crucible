"""THROWAWAY (Phase 5 spike, Q2'). Per test: statements covered and distinct backend/ modules touched, from the
coverage-context DB. Tests are keyed path::function (parametrized node ids collapse; Q1 fix). Finds the knee of the
modules-touched curve and writes {test: {statements, modules}} plus the line -> tests map keyed path::function.

    python q2b_behaviour.py COVERAGE_FILE OUT_DIR
"""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[2]
data = CoverageData(basename=sys.argv[1])
data.read()
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)


def key(ctx: str) -> str | None:
    """'tests/test_x.py::test_name[param]|run' -> 'tests/test_x.py::test_name' (class::method kept)."""
    if not ctx or "::" not in ctx:
        return None
    node = ctx.split("|")[0]
    return re.sub(r"\[.*\]$", "", node)


files = {}
for f in data.measured_files():
    p = Path(f).resolve()
    if p.is_relative_to(ROOT) and p.relative_to(ROOT).as_posix().startswith("backend/"):
        files[f] = p.relative_to(ROOT).as_posix()

stmts: dict[str, set] = defaultdict(set)
line_map: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
for f, rel in files.items():
    for ln, ctxs in data.contexts_by_lineno(f).items():
        for c in ctxs:
            k = key(c)
            if k:
                stmts[k].add((rel, ln))
                line_map[rel][str(ln)].add(k)

per = {t: {"statements": len(s), "modules": len({r for r, _ in s})} for t, s in stmts.items()}
mods = sorted(v["modules"] for v in per.values())
sts = sorted(v["statements"] for v in per.values())
q = lambda xs, p: xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]  # noqa: E731


def knee(ys):
    """Kneedle-style: the point of a sorted curve farthest from the chord between its ends."""
    n = len(ys)
    x0, y0, x1, y1 = 0, ys[0], n - 1, ys[-1]
    best, bi = -1, 0
    for i, y in enumerate(ys):
        d = abs((y1 - y0) * i - (x1 - x0) * y + x1 * y0 - y1 * x0)
        if d > best:
            best, bi = d, i
    return ys[bi]


hist = defaultdict(int)
for m in mods:
    hist[m] += 1
k = knee(mods)
print(json.dumps({
    "tests_keyed_path_function": len(per),
    "modules_touched": {"min": mods[0], "median": statistics.median(mods), "p75": q(mods, .75), "p90": q(mods, .9), "max": mods[-1],
                        "histogram": dict(sorted(hist.items()))},
    "statements_covered": {"min": sts[0], "median": statistics.median(sts), "p75": q(sts, .75), "p90": q(sts, .9), "max": sts[-1]},
    "knee_modules": k,
    "tests_at_or_above_knee": sum(1 for m in mods if m >= k),
}, indent=1))
(out / "per_test.json").write_text(json.dumps(per, indent=1))
(out / "line_map_fn.json").write_text(json.dumps({r: {ln: sorted(t) for ln, t in m.items()} for r, m in line_map.items()}))
