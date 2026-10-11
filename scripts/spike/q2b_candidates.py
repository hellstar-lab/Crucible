"""THROWAWAY (Phase 5 spike, Q2'). Restrict cosmic-ray candidates to the operator whitelist and to lines covered by at
least one BEHAVIOUR test (>= BEHAVIOUR_MODULES distinct backend/ modules), on top of Q2's filters (authored per
backend/evidence/blame.py, not excluded per backend/evidence/exclusions.py). Tests keyed path::function.

    python q2b_candidates.py SESSION_SQLITE Q2B_DIR OUT_JSON
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

from cosmic_ray.work_db import WorkDB, use_db

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.evidence import exclusions  # noqa: E402
from backend.evidence.blame import blame_file  # noqa: E402
from backend.evidence.counting import count_file  # noqa: E402

BEHAVIOUR_MODULES = 7
CANDIDATE_EMAILS = {"abhinavbansal2120@gmail.com"}
_ORDER = {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}


def whitelisted(op: str) -> str | None:
    """The whitelist family, or None."""
    name = op.split("/")[-1]
    m = re.fullmatch(r"ReplaceComparisonOperator_(\w+)_(\w+)", name)
    if m:
        a, b = m.groups()
        if a in _ORDER and b in _ORDER:
            return "comparison swap"
        if {a, b} == {"Is", "IsNot"}:
            return "is / is not"
        return None
    return {"ReplaceTrueWithFalse": "boolean replace", "ReplaceFalseWithTrue": "boolean replace", "AddNot": "not inserted",
            "ReplaceUnaryOperator_Delete_Not": "not removed", "ReplaceAndWithOr": "and / or", "ReplaceOrWithAnd": "and / or",
            "ReplaceBreakWithContinue": "break <-> continue", "ReplaceContinueWithBreak": "break <-> continue",
            "NumberReplacer": "number +/-1"}.get(name)


session, q2b, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
per = json.loads((q2b / "per_test.json").read_text())
line_map = json.loads((q2b / "line_map_fn.json").read_text())
behaviour = {t for t, v in per.items() if v["modules"] >= BEHAVIOUR_MODULES}

items = []
with use_db(session, WorkDB.Mode.open) as db:
    for wi in db.work_items:
        m = wi.mutations[0]
        items.append({"job_id": wi.job_id, "path": Path(m.module_path).resolve().relative_to(ROOT).as_posix(),
                      "operator": m.operator_name, "occurrence": m.occurrence, "start": list(m.start_pos), "end": list(m.end_pos)})
steps = {"raw": len(items)}
items = [dict(i, family=whitelisted(i["operator"])) for i in items]
items = [i for i in items if i["family"]]
steps["whitelist"] = len(items)

authored = {}
for path in sorted({i["path"] for i in items}):
    fc = count_file(ROOT / path, path, None, root=None)
    fb = blame_file(ROOT, fc, lambda e: e.lower() in CANDIDATE_EMAILS) if fc.counted else None
    authored[path] = {ln for a, b in (fb.authored_ranges if fb else []) for ln in range(a, b + 1)}
lines = lambda i: range(i["start"][0], i["end"][0] + 1)  # noqa: E731
items = [i for i in items if all(ln in authored[i["path"]] for ln in lines(i))]
steps["authored"] = len(items)
attrs = exclusions.GitAttributes.parse((ROOT / ".gitattributes").read_text()) if (ROOT / ".gitattributes").exists() else None
items = [i for i in items if exclusions.path_reason(i["path"], attrs) is None
         and exclusions.content_reason(i["path"], (ROOT / i["path"]).read_text()) is None]
steps["not_excluded"] = len(items)
keep = []
for i in items:
    tests = sorted({t for ln in lines(i) for t in line_map.get(i["path"], {}).get(str(ln), [])})
    beh = [t for t in tests if t in behaviour]
    if beh:
        keep.append(dict(i, covering_tests=tests, behaviour_tests=beh))
steps["behaviour_covered"] = len(keep)
print(json.dumps({"behaviour_threshold_modules": BEHAVIOUR_MODULES, "behaviour_tests": len(behaviour), "steps": steps,
                  "by_family": Counter(i["family"] for i in keep).most_common(),
                  "distinct_lines": len({(i["path"], i["start"][0]) for i in keep}),
                  "files": len({i["path"] for i in keep})}, indent=1))
out.write_text(json.dumps(keep))
