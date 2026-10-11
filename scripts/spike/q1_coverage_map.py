"""THROWAWAY (Phase 5 spike). Read a coverage.py database written with --cov-context=test and build
line -> covering tests for backend/. Prints the Q1 distribution and writes the map as JSON.

    python q1_coverage_map.py COVERAGE_FILE OUT_JSON
"""
import json
import statistics
import sys
from pathlib import Path

from coverage import Coverage, CoverageData

ROOT = Path(__file__).resolve().parents[2]
data_file, out = sys.argv[1], Path(sys.argv[2])
data = CoverageData(basename=data_file)
data.read()
cov = Coverage(data_file=data_file)
cov.load()

line_map: dict[str, dict[int, list[str]]] = {}
statements_total = zero = import_only = 0
counts = []
for f in sorted(data.measured_files()):
    rel = Path(f).resolve().relative_to(ROOT).as_posix() if Path(f).resolve().is_relative_to(ROOT) else f
    if not rel.startswith("backend/"):
        continue
    _fn, statements, _excl, missing, _fmt = cov.analysis2(f)
    by_line = data.contexts_by_lineno(f)
    m = {}
    for ln in statements:
        tests = sorted({c.split("|")[0] for c in by_line.get(ln, []) if c and "::" in c})
        if tests:
            m[ln] = tests
        elif by_line.get(ln):
            import_only += 1  # executed, but only outside any test context (module import time)
        counts.append(len(tests))
    statements_total += len(statements)
    zero += sum(1 for ln in statements if ln not in m)
    line_map[rel] = {str(k): v for k, v in m.items()}

covered = [c for c in counts if c]
q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else None  # noqa: E731
print(json.dumps({
    "backend_files": len(line_map), "statements": statements_total,
    "statements_with_no_covering_test": zero, "of_which_executed_only_at_import": import_only,
    "covering_tests_per_statement_all": {"median": statistics.median(counts), "p90": q(counts, 0.9), "max": max(counts)},
    "covering_tests_per_covered_statement": {"median": statistics.median(covered), "p90": q(covered, 0.9)},
}, indent=1))
out.write_text(json.dumps(line_map))
