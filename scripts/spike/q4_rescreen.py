"""THROWAWAY (Phase 5 round 4). Re-screen an existing pool through the format gate and the plant-time form check.

    python q4_rescreen.py POOL_JSON
Pool entries generated before round 4 carry no occurrence, so it is recovered by applying the operator at successive
occurrences to the file at the pool's commit until the mutated line matches.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bugpool.fingerprint import PlantError, check_diff, format_gate  # noqa: E402

PY = Path(sys.executable)
RUFF, CR = str(PY.with_name("ruff")), str(PY.with_name("cosmic-ray"))
pool = json.loads(Path(sys.argv[1]).read_text())
survivors = 0
for c in pool["candidates"]:
    original = subprocess.run(["git", "show", f"{pool['head_sha']}:{c['path']}"], cwd=ROOT, capture_output=True, text=True).stdout
    occ = c.get("occurrence")
    if occ is None:
        for k in range(0, 400):
            with tempfile.TemporaryDirectory() as tmp:
                f = Path(tmp) / Path(c["path"]).name
                f.write_text(original)
                r = subprocess.run([CR, "apply", f.name, c["operator"], str(k)], cwd=tmp, capture_output=True, text=True)
                if r.returncode != 0:
                    break
                if f.read_text().splitlines()[c["line"] - 1] == c["mutated_line"]:
                    occ = k
                    break
    fmt_ok, fmt_why = format_gate(RUFF, CR, c["path"], original, c["operator"], occ) if occ is not None else (False, "occurrence not recovered")
    try:
        check_diff(c["diff"])
        plant_ok, plant_why = True, ""
    except PlantError as exc:
        plant_ok, plant_why = False, str(exc)
    ok = fmt_ok and plant_ok
    survivors += ok
    print(f"{'SURVIVES' if ok else 'REJECTED':<9} {c['path']}:{c['line']}  occ={occ}  {c['mutated_line'].strip()[:70]!r}")
    if not fmt_ok:
        print(f"           format gate: {fmt_why[:160]}")
    if not plant_ok:
        print(f"           plant check: {plant_why[:160]}")
print(f"\n{survivors} of {len(pool['candidates'])} survive")
