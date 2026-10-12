"""Candidate mutations: cosmic-ray ``init`` only (enumerate, execute nothing), then deterministic filters."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from backend.evidence import exclusions
from backend.evidence.blame import blame_file
from backend.evidence.counting import count_file

_ORDER = {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}
FAMILIES = {"ReplaceTrueWithFalse": "boolean replace", "ReplaceFalseWithTrue": "boolean replace", "AddNot": "not inserted",
            "ReplaceUnaryOperator_Delete_Not": "not removed", "ReplaceAndWithOr": "and / or", "ReplaceOrWithAnd": "and / or",
            "ReplaceBreakWithContinue": "break <-> continue", "ReplaceContinueWithBreak": "break <-> continue",
            "NumberReplacer": "number +/-1"}


def family(operator: str) -> str | None:
    """The whitelist family a cosmic-ray operator belongs to, or None (not human-plausible: arithmetic to bitwise,
    cross-type comparisons that only raise TypeError, ...)."""
    name = operator.split("/")[-1]
    m = re.fullmatch(r"ReplaceComparisonOperator_(\w+)_(\w+)", name)
    if m:
        a, b = m.groups()
        if a in _ORDER and b in _ORDER:
            return "comparison swap"
        return "is / is not" if {a, b} == {"Is", "IsNot"} else None
    return FAMILIES.get(name)


@dataclass
class Candidate:
    job_id: str
    path: str
    operator: str
    occurrence: int
    start: tuple[int, int]
    end: tuple[int, int]
    family: str
    covering_tests: list[str] = field(default_factory=list)
    behaviour_tests: list[str] = field(default_factory=list)

    @property
    def lines(self) -> range:
        return range(self.start[0], self.end[0] + 1)


def enumerate_candidates(repo: Path, source: str, python_bin: Path) -> tuple[list[Candidate], dict]:
    """cosmic-ray init into a temporary session: every candidate mutation of ``source``; nothing is executed."""
    from cosmic_ray.work_db import WorkDB, use_db

    cr = python_bin.with_name("cosmic-ray")
    with tempfile.TemporaryDirectory(prefix="bugpool-cr-") as tmp:
        cfg, db = Path(tmp) / "cr.toml", Path(tmp) / "session.sqlite"
        cfg.write_text(f'[cosmic-ray]\nmodule-path = "{source}"\ntimeout = 120.0\nexcluded-modules = []\n'
                       'test-command = "python -m pytest -q -x"\n\n[cosmic-ray.distributor]\nname = "local"\n')
        r = subprocess.run([str(cr), "init", str(cfg), str(db)], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0 or not db.exists():
            return [], {"init_error": (r.stderr or r.stdout)[-500:]}
        out = []
        with use_db(str(db), WorkDB.Mode.open) as w:
            for wi in w.work_items:
                m = wi.mutations[0]
                mp = Path(m.module_path)
                mp = (mp if mp.is_absolute() else repo / mp).resolve()  # cosmic-ray stores paths relative to its cwd
                out.append(Candidate(wi.job_id, mp.relative_to(repo.resolve()).as_posix(),
                                     m.operator_name, m.occurrence, tuple(m.start_pos), tuple(m.end_pos), family(m.operator_name) or ""))
    return out, {}


def authored_lines(repo: Path, paths: set[str], is_candidate) -> dict[str, set[int]]:
    """Lines committed under the candidate's identity, per backend/evidence/blame.py (reused, not reimplemented)."""
    out = {}
    for path in sorted(paths):
        fc = count_file(repo / path, path, None, root=None)
        fb = blame_file(repo, fc, is_candidate) if fc.counted else None
        out[path] = {ln for a, b in (fb.authored_ranges if fb else []) for ln in range(a, b + 1)}
    return out


def integer_literal_at(repo: Path, c: Candidate) -> bool:
    """Number replacement only on an INTEGER literal: a constant or a subscript index (d[0], prev[2]). Never a float
    (0.8 - 1 -> -0.19999999999999996, measured) and never a complex literal."""
    line = (repo / c.path).read_text().splitlines()[c.start[0] - 1]
    lit = line[c.start[1]: c.end[1]] if c.start[0] == c.end[0] else ""
    return bool(re.fullmatch(r"\d+", lit.strip()))


def filter_candidates(repo: Path, cands: list[Candidate], cmap, is_candidate, behaviour_modules: int) -> tuple[list[Candidate], dict]:
    steps = {"raw": len(cands)}
    cands = [c for c in cands if c.family]
    steps["whitelist"] = len(cands)
    cands = [c for c in cands if c.family != "number +/-1" or integer_literal_at(repo, c)]
    steps["integer_literals_only"] = len(cands)
    authored = authored_lines(repo, {c.path for c in cands}, is_candidate)
    cands = [c for c in cands if all(ln in authored.get(c.path, set()) for ln in c.lines)]
    steps["authored"] = len(cands)
    ga = repo / ".gitattributes"
    attrs = exclusions.GitAttributes.parse(ga.read_text()) if ga.exists() else None
    cands = [c for c in cands if exclusions.path_reason(c.path, attrs) is None
             and exclusions.content_reason(c.path, (repo / c.path).read_text()) is None]
    steps["not_excluded"] = len(cands)
    behaviour = cmap.behaviour_tests(behaviour_modules)
    kept = []
    for c in cands:
        tests = sorted(cmap.tests_for(c.path, c.start[0], c.end[0]))
        beh = [t for t in tests if t in behaviour]
        if beh:
            c.covering_tests, c.behaviour_tests = tests, beh
            kept.append(c)
    steps["behaviour_covered"] = len(kept)
    steps["behaviour_tests"] = len(behaviour)
    return kept, steps
