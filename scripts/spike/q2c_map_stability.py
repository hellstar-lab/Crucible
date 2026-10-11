"""THROWAWAY (Phase 5). Build the path::function line map of Crucible three times (in a worktree) and compare."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bugpool.coverage_map import build  # noqa: E402

with tempfile.TemporaryDirectory(prefix="mapstab-") as tmp:
    wt = Path(tmp) / "wt"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt), "HEAD"], cwd=ROOT, check=True, capture_output=True)
    try:
        maps = [build(wt, "backend", sys.executable) for _ in range(3)]
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
js = [m.as_json() for m in maps]
keys = [sorted({t for f in j.values() for ts in f.values() for t in ts}) for j in js]
print(json.dumps({"collected": [m.collected for m in maps], "exit_codes": [m.exit_code for m in maps],
                  "lines_mapped": [sum(len(f) for f in j.values()) for j in js], "test_keys": [len(k) for k in keys],
                  "identical_maps": js[0] == js[1] == js[2], "keys_with_brackets": sum("[" in t for t in keys[0])}, indent=1))
