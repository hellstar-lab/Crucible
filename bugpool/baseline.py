"""The environment precondition and the baseline stability gate.

precondition   run the suite ONCE, unmodified, and say which of two things is true: the suite could not run (an
               import error, a missing database, a wrong interpreter; with the underlying error), or it ran. Only a
               suite that ran can be judged on coverage; "could not run" is never reported as insufficient coverage.
stability      run it N times (the first run is the precondition's); a test whose result is not identical on every
               run is dropped from the ORACLE set, and so is a test that fails on the unmodified code. Only oracle
               tests can cover or kill a candidate.

Results are keyed path::function like the coverage map; a parametrized function's result is the sorted list of its
cases' outcomes (ids dropped: a random parameter changes the id every run, Q1).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import COULD_NOT_RUN

_PARAMS = re.compile(r"\[.*$")
_SKIP_REASON = re.compile(r"^SKIPPED \[\d+\] (\S+?):\d+: (.*)$", re.M)


@dataclass
class SuiteRun:
    ran: bool
    exit_code: int | None = None
    results: dict[str, list[str]] = field(default_factory=dict)  # test key -> sorted outcomes of its cases
    skipped: list[dict] = field(default_factory=list)  # [{test, reason}]
    error: str | None = None
    seconds: float = 0.0

    @property
    def collected(self) -> int:
        return sum(len(v) for v in self.results.values())

    def count(self, outcome: str) -> int:
        return sum(v.count(outcome) for v in self.results.values())


def junit_results(xml: Path) -> tuple[dict[str, list[str]], list[dict], bool]:
    """(key -> outcomes, skipped cases, collection error seen)."""
    results: dict[str, list[str]] = {}
    skipped, collection_error = [], False
    for tc in ET.parse(xml).getroot().iter("testcase"):
        if not tc.get("name"):
            collection_error = True
            continue
        key = f"{tc.get('classname', '').replace('.', '/')}.py::{_PARAMS.sub('', tc.get('name'))}"
        if tc.find("failure") is not None:
            out = "failed"
        elif tc.find("error") is not None:
            out = "error"
        elif tc.find("skipped") is not None:
            out = "skipped"
            sk = tc.find("skipped")
            skipped.append({"test": key, "reason": (sk.get("message") or "")[:200]})
        else:
            out = "passed"
        results.setdefault(key, []).append(out)
    return {k: sorted(v) for k, v in results.items()}, skipped, collection_error


def _tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    errs = [ln for ln in lines if re.match(r"^(E |ERROR|ImportError|ModuleNotFoundError|.*Error:)", ln)]
    return "\n".join((errs or lines)[-n:])[-1500:]


def run_suite(repo: Path, python: str, test_args: list[str] | None = None, timeout_s: int = 3600) -> SuiteRun:
    """One run of the whole suite, unmodified. ``ran`` is False with the underlying error when the suite could not run."""
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bugpool-base-") as tmp:
        xml = Path(tmp) / "j.xml"
        try:
            p = subprocess.run([python, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", f"--junitxml={xml}", *(test_args or [])],
                               cwd=repo, capture_output=True, text=True, timeout=timeout_s)
        except FileNotFoundError as e:
            return SuiteRun(False, None, error=f"the test interpreter does not exist: {e.filename}", seconds=0.0)
        except subprocess.TimeoutExpired:
            return SuiteRun(False, None, error=f"the suite did not finish within {timeout_s} s", seconds=float(timeout_s))
        secs = round(time.monotonic() - t0, 1)
        out = p.stdout + p.stderr
        if p.returncode in (2, 3, 4) or not xml.exists():
            what = {2: "collection or import error", 3: "pytest internal error", 4: "pytest usage error"}.get(p.returncode, "no result file")
            return SuiteRun(False, p.returncode, error=f"{what} (exit {p.returncode}): {_tail(out)}", seconds=secs)
        results, skipped, collection_error = junit_results(xml)
        reasons = dict(_SKIP_REASON.findall(p.stdout))
        for s in skipped:  # pytest's -rs line carries the reason when junit's message is empty
            s["reason"] = s["reason"] or reasons.get(s["test"].split("::")[0], "")
    run = SuiteRun(True, p.returncode, results, skipped, seconds=secs)
    if collection_error:
        run.ran, run.error = False, f"collection error: {_tail(out)}"
    elif results and run.count("passed") == 0 and run.count("error") > 0:
        # every test errored before its body ran: the environment (a database, a service), not the code
        run.ran, run.error = False, f"every test errored in setup, none passed: {_tail(out)}"
    return run


def precondition(run: SuiteRun) -> dict:
    """{status: ran | could_not_run, ...}: which of the two refusals applies, decided before any coverage question."""
    if not run.ran:
        return {"status": "could_not_run", "class": COULD_NOT_RUN, "error": run.error, "exit_code": run.exit_code,
                "seconds": run.seconds}
    return {"status": "ran", "exit_code": run.exit_code, "collected": run.collected, "passed": run.count("passed"),
            "failed": run.count("failed"), "errors": run.count("error"), "skipped": run.skipped, "seconds": run.seconds}


def stability(runs: list[SuiteRun]) -> dict:
    """The oracle set: tests with an IDENTICAL result on every run that also pass on the unmodified code."""
    keys = set().union(*(r.results for r in runs))
    unstable = {k: [r.results.get(k, ["absent"]) for r in runs] for k in sorted(keys)
                if any(r.results.get(k) != runs[0].results.get(k) for r in runs)}
    failing = sorted(k for k in keys if k not in unstable and any(o != "passed" for o in runs[0].results.get(k, [])))
    oracle = sorted(k for k in keys if k not in unstable and k not in failing)
    return {"runs": len(runs), "oracle": oracle, "unstable": unstable, "not_passing_at_baseline": failing,
            "rule": f"a test with a non-identical result across {len(runs)} baseline runs, or one that does not pass on the "
                    "unmodified code, is not an oracle: it can neither cover nor kill a candidate"}
