"""Session side: plant ONE pre-validated bug from a pool, or plant nothing and say why. No search happens here."""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from . import INSUFFICIENT


def plant(pool, workdir: Path, *, seed: int | None = None) -> dict:
    """{"planted": bool, "message": str, "candidate": dict | None}. ``workdir`` is a checkout at the pool's commit."""
    pool = json.loads(Path(pool).read_text()) if isinstance(pool, (str, Path)) else pool
    if pool.get("status") != "ok" or not pool.get("candidates"):
        return {"planted": False, "message": INSUFFICIENT, "reason": pool.get("reason"), "candidate": None}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True).stdout.strip()
    if head != pool["head_sha"]:
        return {"planted": False, "message": f"the pool was generated for {pool['head_sha'][:12]}, the checkout is {head[:12]}",
                "reason": "commit mismatch", "candidate": None}
    if subprocess.run(["git", "diff", "--quiet"], cwd=workdir).returncode != 0:
        return {"planted": False, "message": "the checkout has local changes; refusing to plant", "reason": "dirty tree", "candidate": None}
    cand = random.Random(seed).choice(pool["candidates"])
    r = subprocess.run(["git", "apply", "-"], cwd=workdir, input=cand["diff"], capture_output=True, text=True)
    if r.returncode != 0:
        return {"planted": False, "message": f"the pooled diff no longer applies: {r.stderr.strip()[:200]}", "reason": "apply failed",
                "candidate": None}
    return {"planted": True, "message": f"planted one validated bug in {cand['path']}", "candidate": cand}
