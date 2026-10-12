"""python -m bugpool generate --repo PATH --email ADDR [--email ...] --out pool.json [--target 20] [--max-validations 300]
python -m bugpool session  --repo PATH --pool pool.json --out record.json [--stranger results.json] [--budget 40]

The session is a plain terminal REPL (the simplest surface to demo):
  tree [PREFIX]          the repository's files (free: the participant is shown the repo; not an action)
  open PATH [START]      FILE_OPEN
  search PATTERN [PATH]  SEARCH (regex; PATH limits it to a file or directory)
  test [SELECTOR]        TEST_RUN (no selector = the whole suite)
  answer PATH LINE       ANSWER (ends the hunt)
  quit                   end without answering (recorded as not found within budget)
"""

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
    ss = sub.add_parser("session")
    ss.add_argument("--repo", type=Path, required=True)
    ss.add_argument("--pool", type=Path, required=True)
    ss.add_argument("--out", type=Path, required=True)
    ss.add_argument("--stranger", type=Path, help="the stranger's results, for the same-candidate curve")
    ss.add_argument("--budget", type=int, default=40)
    ss.add_argument("--seed", type=int)
    a = ap.parse_args()
    if a.cmd == "session":
        return repl(a)
    pool = generate_pool(a.repo, a.email, source=a.source, behaviour_modules=a.behaviour_modules, target=a.target,
                         max_validations=a.max_validations, seed=a.seed)
    write(pool, a.out)
    print(f"{pool['status']}: {len(pool['candidates'])} candidates" + (f" ({pool['reason']})" if pool.get("reason") else ""))
    return 0 if pool["status"] == "ok" else 1


def repl(a) -> int:
    import shlex

    from .session import Session

    s = Session.start(a.repo, a.pool, seed=a.seed, budget=a.budget)
    if s.refused:
        rec = s.end(a.out)
        print(f"{rec['message']} ({rec['reason']}). Nothing was planted.")
        return 1
    v = s.view()
    print(f"A test fails: {v['failing_test']}\n\n{v['failing_output']}\n")
    print(f"{len(v['files'])} files in the repository ('tree' to list). Budget: {v['budget']} actions before your answer.")
    while s.outcome is None:
        try:
            parts = shlex.split(input("> "))
        except (EOFError, ValueError):
            break
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        if cmd == "tree":
            print("\n".join(f for f in v["files"] if not args or f.startswith(args[0])))
        elif cmd == "open" and args:
            print(s.file_open(args[0], int(args[1]) if len(args) > 1 else None))
        elif cmd == "search" and args:
            print(s.search(args[0], args[1] if len(args) > 1 else None))
        elif cmd == "test":
            print(s.test_run(args[0] if args else None))
        elif cmd == "answer" and len(args) == 2 and args[1].isdigit():
            s.answer(args[0], int(args[1]))
        elif cmd == "quit":
            break
        else:
            print("commands: tree [PREFIX] | open PATH [START] | search PATTERN [PATH] | test [SELECTOR] | answer PATH LINE | quit")
    rec = s.end(a.out, a.stranger)
    o = rec["outcome"]
    print(f"\n{o['status']}: {o['actions_used']} actions, {o['distinct_files_opened']} files opened. Record written to {a.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
