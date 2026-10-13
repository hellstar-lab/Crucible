"""python -m bugpool generate --repo PATH --email ADDR [--email ...] --out pool.json [--target 20] [--max-validations 300]
python -m bugpool session  --repo PATH --pool pool.json --out record.json [--stranger results.json] [--budget 40]
                           [--condition code|docs] [--python TEST_PYTHON] [--registry burned.json]
python -m bugpool report   record.json --out page.html

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
    g.add_argument("--python", help="the interpreter that runs the repository's tests (default: this one)")
    g.add_argument("--registry", type=Path, help="burned-line registry (default <repo>/.bugpool/burned.json)")
    g.add_argument("--baseline-runs", type=int, default=3)
    g.add_argument("--kill-runs", type=int, default=3)
    g.add_argument("--max-per-killing-test", type=int, default=2)
    g.add_argument("--max-per-module", type=int, default=3)
    g.add_argument("--claims", type=Path, help="the Phase 4 claim-verdict record: validate claimed modules first")
    g.add_argument("--single-commit-override", metavar="REASON",
                   help="pool a repository whose sources have one commit anyway; REASON is recorded in the pool")
    ss = sub.add_parser("session")
    ss.add_argument("--repo", type=Path, required=True)
    ss.add_argument("--pool", type=Path, required=True)
    ss.add_argument("--out", type=Path, required=True)
    ss.add_argument("--stranger", type=Path, help="the stranger's results, for the same-candidate curve")
    ss.add_argument("--budget", type=int, default=40)
    ss.add_argument("--seed", type=int)
    ss.add_argument("--condition", choices=["code", "docs"], help="visible file set: code = docs/ hidden, docs = all shown")
    ss.add_argument("--python", help="the interpreter that runs the repository's tests")
    ss.add_argument("--registry", type=Path, help="burned-line registry (default <repo>/.bugpool/burned.json)")
    ss.add_argument("--claims", type=Path, help="the Phase 4 claim-verdict record: plant in a module the résumé claims")
    ss.add_argument("--allow-uncited", action="store_true", help="with --claims: plant in an uncited module when no claimed one has a bug")
    bp = sub.add_parser("burn", help="seed the burned-line registry from what has already been shown")
    bp.add_argument("--repo", type=Path, required=True)
    bp.add_argument("--text", type=Path, action="append", default=[])
    bp.add_argument("--json", type=Path, action="append", default=[])
    bp.add_argument("--git", action="store_true", help="also every commit message")
    bp.add_argument("--registry", type=Path, help="default <repo>/.bugpool/burned.json")
    bp.add_argument("--source", default="backend", help="directory, or comma-separated directories, the mentions refer to")
    rp = sub.add_parser("report")
    rp.add_argument("record", type=Path)
    rp.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "session":
        return repl(a)
    if a.cmd == "burn":
        import json as _json

        from . import burned
        reg = burned.Registry(a.registry or a.repo / burned.DEFAULT)
        for t in a.text:
            for src in a.source.split(","):
                burned.burn_text(reg, a.repo, t.read_text(), str(t), source=src)
        for j in a.json:
            burned.burn_json(reg, _json.loads(j.read_text()), str(j))
        if a.git:
            for src in a.source.split(","):
                burned.burn_git_log(reg, a.repo, source=src)
        reg.save()
        print(f"{len(reg)} burned lines in {reg.path}")  # a count only
        return 0
    if a.cmd == "report":
        from .report import write as write_report
        print(write_report(a.record, a.out))
        return 0
    pool = generate_pool(a.repo, a.email, source=a.source, behaviour_modules=a.behaviour_modules, target=a.target,
                         max_validations=a.max_validations, seed=a.seed, python=a.python, burned_registry=a.registry,
                         baseline_runs=a.baseline_runs, kill_runs=a.kill_runs, max_per_killing_test=a.max_per_killing_test,
                         max_per_module=a.max_per_module, single_commit_override=a.single_commit_override, claims=a.claims,
                         log=lambda *x: print(*x, flush=True))  # a long run's log is readable while it runs
    write(pool, a.out)
    print(f"{pool['status']}: {len(pool['candidates'])} candidates"
          + (f" ({pool['refusal']}: {pool['reason']})" if pool.get("reason") else ""))
    return 0 if pool["status"] == "ok" else 1


def repl(a) -> int:
    import shlex

    from .session import Session

    s = Session.start(a.repo, a.pool, seed=a.seed, budget=a.budget, python=a.python, condition=a.condition,
                      claims=a.claims, allow_uncited=a.allow_uncited)
    if s.refused:
        rec = s.end(a.out, registry=a.registry)
        print(f"{rec['message']} ({rec['reason']}). Nothing was planted.")
        return 1
    v = s.view()
    print(f"A test fails: {v['failing_test']}\n\n{v['failing_output']}\n")
    print(f"{len(v['files'])} files in the repository ('tree' to list). Budget: {v['budget']} actions before your answer."
          + (f" Condition: {a.condition}." if a.condition else ""))
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
    rec = s.end(a.out, a.stranger, registry=a.registry)
    o = rec["outcome"]
    print(f"\n{o['status']}: {o['actions_used']} actions, {o['distinct_files_opened']} files opened. Record written to {a.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
