"""THROWAWAY (Phase 5, concurrent_reg). Read the sealed pool and print AGGREGATES ONLY: no module, line, operator, diff,
killing test or candidate id reaches stdout. Per-candidate facts are printed by ORDINAL only. Pick one candidate per
session condition (code, docs), each in a different module, and write the selections to the sealed directory only.

    python q6_creg_seal.py [CREG_HOME]
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

H = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".bugpool" / "concurrent_reg"
SEALED = H / "sealed"
pool = json.loads((SEALED / "pool.json").read_text())
cands = pool["candidates"]
st = pool.get("stats", {})
per_test = Counter(c["killing_test"] for c in cands)
per_mod = Counter(c["path"] for c in cands)
reg = burned.Registry(H / "burned.json")
v = st.get("validated", {})
orc = pool.get("oracle", {})
pre = pool.get("precondition", {})
print(json.dumps({
    "status": pool["status"], "refusal": pool.get("refusal"), "reason": pool.get("reason"),
    "authorship": {k: pool.get("authorship", {}).get(k) for k in ("commits", "established")}
                  | {"override_recorded": bool(pool.get("authorship", {}).get("override"))},
    "precondition": {k: pre.get(k) for k in ("status", "collected", "passed", "failed", "errors")} | {"skipped": len(pre.get("skipped", []))},
    "oracle": {"size": orc.get("size"), "baseline_runs": orc.get("runs"), "unstable_tests": sorted(orc.get("unstable", {})),
               "not_passing_at_baseline": orc.get("not_passing_at_baseline")},
    "behaviour_tests": st.get("behaviour_tests"), "modules_per_oracle_test": st.get("modules_per_oracle_test"),
    "filters": st.get("filters"),
    "size": len(cands),
    "killing_test_spread": sorted(per_test.values(), reverse=True), "distinct_killing_tests": len(per_test),
    "module_spread": sorted(per_mod.values(), reverse=True), "distinct_modules": len(per_mod),
    "restated_gate": {"15_candidates": len(cands) >= 15, "5_killing_tests": len(per_test) >= 5, "3_modules": len(per_mod) >= 3},
    "validations": v, "rejected_for_instability": st.get("rejected_unstable", 0),
    "every_kill_repeated_3_times": all(len(c.get("kill_runs", [])) == 3 and all(r == [c["killing_test"]] for r in c["kill_runs"])
                                       for c in cands),
    "every_candidate_lint_clean": all(c["checks"]["plausibility"].get("lint_clean") for c in cands),
    "every_candidate_format_stable": all(c["checks"]["plausibility"].get("format_stable") for c in cands),
    "every_candidate_integer_literal_only": all(c["checks"]["plausibility"].get("integer_literal_only") for c in cands),
    "unseen_discipline": pool.get("unseen_discipline"),
    "any_candidate_burned_now": any(reg.burned(c["path"], c["line"], c.get("original_line"), c.get("id")) for c in cands),
    "dropped_for_diversity": st.get("dropped_for_diversity"),
    "docs_telegraph": st.get("docs_telegraph"),
    "seconds": st.get("seconds"),
}, indent=1))
for k, c in enumerate(cands, 1):  # by ordinal only
    d = c["checks"]["docs_telegraph"]
    print(f"  candidate #{k:>2}: described={'yes' if d['described'] else 'no '}  in docs/ (hidden under 'code'): "
          f"{d['hits_in_hidden_dirs']:>2}  in prose visible in both (README, docstrings): {d['hits_visible_in_both']:>2}")
if pool["status"] != "ok" or len(per_mod) < 2:
    sys.exit(0)
rng = random.SystemRandom()
chosen = {}
while True:  # one seed per condition; Session.start(seed=s) picks random.Random(s).choice(candidates)
    seeds = {cond: secrets.randbelow(10**9) for cond in ("code", "docs")}
    chosen = {cond: random.Random(s).choice(cands) for cond, s in seeds.items()}
    if chosen["code"]["path"] != chosen["docs"]["path"]:  # different modules: the first hunt must not teach the second
        break
for cond, c in chosen.items():
    (SEALED / f"seed_{cond}.txt").write_text(str(seeds[cond]))
(SEALED / "selection.json").write_text(json.dumps({cond: {"session_seed": seeds[cond], "candidate_id": candidate_id(c), "module": c["path"],
                                                          "line": c["line"], "operator": c["operator"], "diff": c["diff"],
                                                          "killing_test": c["killing_test"]} for cond, c in chosen.items()}, indent=1))
print("selections sealed (one per condition, different modules)")
