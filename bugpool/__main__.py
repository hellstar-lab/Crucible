"""python -m bugpool generate --repo PATH --email ADDR [--email ...] --out pool.json [--target 20] [--max-validations 300]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pool import generate_pool, write


def main() -> int:
    ap = argparse.ArgumentParser(prog="bugpool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--repo", type=Path, required=True)
    g.add_argument("--email", action="append", required=True, help="the candidate's commit identity (repeatable)")
    g.add_argument("--source", default="backend")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--behaviour-modules", type=int, default=7)
    g.add_argument("--target", type=int, default=20)
    g.add_argument("--max-validations", type=int, default=300)
    g.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    pool = generate_pool(a.repo, a.email, source=a.source, behaviour_modules=a.behaviour_modules, target=a.target,
                         max_validations=a.max_validations, seed=a.seed)
    write(pool, a.out)
    print(f"{pool['status']}: {len(pool['candidates'])} candidates" + (f" ({pool['reason']})" if pool.get("reason") else ""))
    return 0 if pool["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
