"""Validate one candidate: apply it, run ONLY its covering tests, revert, classify. Always reverts; asserts a clean tree."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

GENERIC = set("""self none true false return items item result results list dict text path line lines name names data value
values key keys test tests with from import else elif lambda len str int float get set append extend join split strip
lower upper format range enumerate sorted isinstance kind type count index start end first last left right page pages
out file files args kwargs config match find search""".split())
_PARAMS = re.compile(r"\[.*$")  # parametrized ids collapse to the test function
_FRAME = re.compile(r"^(\S+\.py):(\d+): (\w+(?:\.\w+)*)\s*$", re.M)
CLEAN = "clean"


LINT_SELECT = "E7,E9,F,SIM,W6"  # explicit: ruff 0.17's default selection reports nothing for `not x is None`


def lint_new(ruff: str, path: str, original: str, mutated: str, lines: range) -> list[str]:
    """Diagnostic codes the mutation INTRODUCES on its line(s): present in the mutated text, absent from the original.
    Catches the unnatural spellings a human would not write (E714 `not x is None`, E713 `not x in`, E712 `== True`,
    SIM201 `not a == b` ...), which a careful hunter could spot by form alone."""
    def diags(text):
        r = subprocess.run([ruff, "check", "--no-cache", "--select", LINT_SELECT, "--output-format", "json", "--stdin-filename",
                            path, "-"], input=text, capture_output=True, text=True)
        try:
            return {(d["code"], d["location"]["row"]) for d in json.loads(r.stdout or "[]")}
        except json.JSONDecodeError:
            return set()
    before = {code for code, row in diags(original) if row in lines}
    return sorted({code for code, row in diags(mutated) if row in lines} - before)


@dataclass
class Validation:
    cls: str  # clean | survivor | noisy | exception_at_site | telegraphed | lint | broken
    diff: str = ""
    failed: list[dict] = field(default_factory=list)
    telegraph_hits: list[str] = field(default_factory=list)
    seconds: float = 0.0
    detail: str = ""


def tree_clean(repo: Path) -> bool:
    return subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0


def enclosing_function(repo: Path, path: str, line: int) -> str | None:
    best = None
    for node in ast.walk(ast.parse((repo / path).read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= node.end_lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best else None


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 4} - GENERIC


def telegraph_terms(repo: Path, path: str, start: int, end: int) -> set[str]:
    """Words that, in a killing test's name or message, would point straight at the mutation: identifiers and string
    literals on the mutated line(s) and two lines either side, and the enclosing function's name."""
    src = (repo / path).read_text().splitlines()
    terms: set[str] = set()
    for ln in src[max(0, start - 3): end + 2]:
        for ident in re.findall(r"[A-Za-z_]\w+", ln):
            terms |= _tokens(ident.replace("_", " "))
            if len(ident) >= 4 and ident.lower() not in GENERIC:
                terms.add(ident.lower())
        for lit in re.findall(r"[\"']([^\"']{4,})[\"']", ln):
            terms |= _tokens(lit)
    func = enclosing_function(repo, path, start)
    if func:
        terms |= {func.lower().strip("_")} | _tokens(func.replace("_", " "))
    return terms - GENERIC


def telegraph_hits(terms: set[str], test: str, message: str) -> list[str]:
    name, msg = _tokens(test.split("::")[-1].replace("_", " ")), _tokens(message)
    return sorted(t for t in terms if t in name or t in msg or (len(t) >= 6 and t in message.lower()))


def validate(repo: Path, cand, python: str, timeout_s: int = 600) -> Validation:
    repo = Path(repo)
    if not tree_clean(repo):
        raise RuntimeError("refusing to mutate a dirty tree")
    cr = str(Path(python).with_name("cosmic-ray"))
    ruff = Path(python).with_name("ruff")
    terms = telegraph_terms(repo, cand.path, cand.start[0], cand.end[0])
    original = (repo / cand.path).read_text()
    try:
        r = subprocess.run([cr, "apply", cand.path, cand.operator, str(cand.occurrence)], cwd=repo, capture_output=True, text=True)
        diff = subprocess.run(["git", "diff", "-U2", "--", cand.path], cwd=repo, capture_output=True, text=True).stdout
        if r.returncode != 0 or not diff:
            return Validation("broken", diff, detail=f"apply failed: {(r.stderr or r.stdout)[-200:]}")
        if ruff.exists():  # the lint gate runs before any test: cheap, and a lint-visible bug is not a fair hunt
            new = lint_new(str(ruff), cand.path, original, (repo / cand.path).read_text(), cand.lines)
            if new:
                return Validation("lint", diff, detail=f"the mutation introduces {', '.join(new)}")
        with tempfile.TemporaryDirectory(prefix="bugpool-v-") as tmp:
            xml = Path(tmp) / "j.xml"
            t0 = time.monotonic()
            try:
                p = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *cand.covering_tests],
                                   cwd=repo, capture_output=True, text=True, timeout=timeout_s)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                return Validation("broken", diff, seconds=timeout_s, detail="hang (timeout)")
            secs = round(time.monotonic() - t0, 1)
            failed: dict[str, dict] = {}
            collection_error = False
            if xml.exists():
                for tc in ET.parse(xml).getroot().iter("testcase"):
                    bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                    if bad is None:
                        continue
                    if not tc.get("name"):
                        collection_error = True
                        continue
                    func = _PARAMS.sub("", tc.get("name"))
                    key = f"{tc.get('classname', '').replace('.', '/')}.py::{func}"
                    frames = _FRAME.findall(bad.text or "")
                    failed.setdefault(key, {"test": key, "message": (bad.get("message") or "")[:500],
                                            "innermost": list(frames[-1]) if frames else None})
        if rc in (2, 3, 4) or collection_error:
            return Validation("broken", diff, list(failed.values()), seconds=secs, detail="collection / import / syntax error")
        if not failed:
            return Validation("survivor", diff, seconds=secs)
        if len(failed) > 1:
            return Validation("noisy", diff, list(failed.values()), seconds=secs)
        f = next(iter(failed.values()))
        inner = f["innermost"]
        if inner and inner[0] == cand.path and cand.start[0] <= int(inner[1]) <= cand.end[0] and not inner[2].endswith("AssertionError"):
            return Validation("exception_at_site", diff, [f], seconds=secs, detail=f"{inner[2]} raised at the mutated line")
        hits = telegraph_hits(terms, f["test"], f["message"])
        if hits:
            return Validation("telegraphed", diff, [f], hits, seconds=secs)
        return Validation(CLEAN, diff, [f], [], seconds=secs)
    finally:
        subprocess.run(["git", "checkout", "--", cand.path], cwd=repo, check=True)
        if not tree_clean(repo):
            raise RuntimeError(f"revert failed for {cand.path}")
