"""THROWAWAY (Phase 5 spike). cosmic-ray candidates (from `cosmic-ray init`, nothing executed) -> operator breakdown
and the three filters: (a) authored by the candidate per backend/evidence/blame.py, (b) >= 1 covering test per the Q1
map, (c) not excluded per backend/evidence/exclusions.py.

    python q2_candidates.py SESSION_SQLITE LINE_MAP_JSON OUT_JSON
"""
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from cosmic_ray.work_db import WorkDB, use_db

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.evidence import exclusions  # noqa: E402
from backend.evidence.blame import blame_file  # noqa: E402
from backend.evidence.counting import count_file  # noqa: E402

CANDIDATE_EMAILS = {"abhinavbansal2120@gmail.com"}  # hellstar-lab, the only identity besides Auratech350 in the log

session, line_map_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
line_map = json.loads(Path(line_map_path).read_text())
items = []
with use_db(session, WorkDB.Mode.open) as db:
    for wi in db.work_items:
        m = wi.mutations[0]
        rel = Path(m.module_path).resolve().relative_to(ROOT).as_posix()
        items.append({"job_id": wi.job_id, "path": rel, "operator": m.operator_name, "occurrence": m.occurrence,
                      "start": list(m.start_pos), "end": list(m.end_pos)})
by_op = Counter(i["operator"] for i in items)
family = Counter(i["operator"].split("/")[-1].split("_")[0] for i in items)

authored: dict[str, set[int]] = {}
with tempfile.TemporaryDirectory() as tmp:
    for path in sorted({i["path"] for i in items}):
        fc = count_file(ROOT / path, path, None, root=None)
        if not fc.counted:
            authored[path] = set()
            continue
        fb = blame_file(ROOT, fc, lambda e: e.lower() in CANDIDATE_EMAILS)
        authored[path] = {ln for a, b in fb.authored_ranges for ln in range(a, b + 1)}

def lines(i):
    return range(i["start"][0], i["end"][0] + 1)

attrs = exclusions.GitAttributes.parse((ROOT / ".gitattributes").read_text()) if (ROOT / ".gitattributes").exists() else None
step = {"raw": len(items)}
a = [i for i in items if all(ln in authored[i["path"]] for ln in lines(i))]
step["a_authored"] = len(a)
b = [i for i in a if any(line_map.get(i["path"], {}).get(str(ln)) for ln in lines(i))]
step["b_covered"] = len(b)
c = [i for i in b if exclusions.path_reason(i["path"], attrs) is None
     and exclusions.content_reason(i["path"], (ROOT / i["path"]).read_text()) is None]
step["c_not_excluded"] = len(c)
for i in c:
    i["covering_tests"] = sorted({t for ln in lines(i) for t in line_map.get(i["path"], {}).get(str(ln), [])})
print(json.dumps({"steps": step, "by_family": family.most_common(), "by_operator_top20": by_op.most_common(20),
                  "operators_total": len(by_op)}, indent=1))
out.write_text(json.dumps(c))
