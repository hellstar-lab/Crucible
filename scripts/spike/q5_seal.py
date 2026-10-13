"""THROWAWAY (Phase 5, unseen live session). Read the sealed pool and print AGGREGATES ONLY: no module, line, operator,
diff, killing test or candidate id ever reaches stdout. Pick the session's candidate with a fresh seed and write the
selection, and a one-candidate pool for the stranger, to reports/sealed/ only.

    python q5_seal.py
"""
import json
import random
import secrets
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bugpool import burned  # noqa: E402
from bugpool.session import candidate_id  # noqa: E402

SEALED = ROOT / "reports" / "sealed"
pool = json.loads((SEALED / "pool.json").read_text())
cands = pool["candidates"]
st = pool.get("stats", {})
per_test = Counter(c["killing_test"] for c in cands)
per_mod = Counter(c["path"] for c in cands)
reg = burned.Registry(ROOT / burned.DEFAULT)
v = st.get("validated", {})
print(json.dumps({
    "status": pool["status"], "reason": pool.get("reason"),
    "size": len(cands),
    "distinct_killing_tests": len(per_test), "max_per_killing_test": max(per_test.values(), default=0),
    "distinct_modules": len(per_mod), "max_per_module": max(per_mod.values(), default=0),
    "restated_gate": {"15_candidates": len(cands) >= 15, "5_killing_tests": len(per_test) >= 5, "3_modules": len(per_mod) >= 3},
    "validations": sum(v.values()), "rejected_by_lint_gate": v.get("lint", 0), "rejected_by_format_gate": v.get("format", 0),
    "every_candidate_lint_clean": all(c["checks"]["plausibility"].get("lint_clean") for c in cands),
    "every_candidate_format_stable": all(c["checks"]["plausibility"].get("format_stable") for c in cands),
    "unseen_discipline": pool.get("unseen_discipline"),
    "any_candidate_burned_now": any(reg.burned(c["path"], c["line"], c.get("original_line"), c.get("id")) for c in cands),
    "dropped_for_diversity": st.get("dropped_for_diversity"),
}, indent=1))
if pool["status"] != "ok" or not cands:
    sys.exit(0)
seed = secrets.randbelow(10**9)
chosen = random.Random(seed).choice(cands)  # exactly Session.start(seed=...)'s choice
(SEALED / "selection.json").write_text(json.dumps({"session_seed": seed, "candidate_id": candidate_id(chosen), "module": chosen["path"],
                                                   "line": chosen["line"], "operator": chosen["operator"], "diff": chosen["diff"],
                                                   "killing_test": chosen["killing_test"]}, indent=1))
(SEALED / "stranger_pool.json").write_text(json.dumps(dict(pool, candidates=[chosen]), indent=1))
(SEALED / "session_seed.txt").write_text(str(seed))
print("selection sealed")
