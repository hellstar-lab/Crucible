"""line -> covering tests, keyed path::function, from a coverage.py run with per-test contexts."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


def test_key(context: str) -> str | None:
    """'tests/test_x.py::TestA::test_b[param]|run' -> 'tests/test_x.py::TestA::test_b'. Parametrized ids collapse to
    their function so the key is stable across runs (a random parameter changes the node id every run)."""
    if not context or "::" not in context:
        return None
    node = context.rsplit("|", 1)[0] if re.search(r"\|(run|setup|teardown)$", context) else context
    head, sep, last = node.rpartition("::")
    return f"{head}{sep}{last.split('[', 1)[0]}"  # a parameter id may itself contain "|" or "]"


@dataclass
class CoverageMap:
    lines: dict[str, dict[int, set[str]]] = field(default_factory=dict)  # path -> line -> {test keys}
    modules: dict[str, set[str]] = field(default_factory=dict)  # test key -> distinct source modules touched
    statements: dict[str, int] = field(default_factory=dict)  # test key -> statements covered
    collected: int = 0
    exit_code: int | None = None

    def tests_for(self, path: str, start: int, end: int) -> set[str]:
        return {t for ln in range(start, end + 1) for t in self.lines.get(path, {}).get(ln, ())}

    def behaviour_tests(self, min_modules: int) -> set[str]:
        return {t for t, m in self.modules.items() if len(m) >= min_modules}

    def as_json(self) -> dict:
        return {p: {str(ln): sorted(ts) for ln, ts in m.items()} for p, m in sorted(self.lines.items())}


def build(repo: Path, source: str, python: str, test_args: list[str] | None = None, timeout_s: int = 3600) -> CoverageMap:
    """Run the repository's suite once with coverage contexts (--cov-context=test) and read the database."""
    from coverage import CoverageData

    repo = Path(repo).resolve()
    with tempfile.TemporaryDirectory(prefix="bugpool-cov-") as tmp:
        data_file = Path(tmp) / ".coverage"
        env = {**os.environ, "COVERAGE_FILE": str(data_file)}
        p = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--cov={source}", "--cov-context=test",
                            "--cov-report=", *(test_args or [])], cwd=repo, env=env, capture_output=True, text=True,
                           timeout=timeout_s)
        cmap = CoverageMap(exit_code=p.returncode)
        summary = next((ln for ln in reversed(p.stdout.splitlines()) if re.search(r" in [\d.]+s", ln)), "")
        cmap.collected = sum(int(x) for x in re.findall(r"(\d+) (?:passed|failed|errors?|skipped|xfailed|xpassed)", summary))
        if not data_file.exists():
            return cmap
        data = CoverageData(basename=str(data_file))
        data.read()
        lines: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        modules: dict[str, set[str]] = defaultdict(set)
        statements: dict[str, int] = defaultdict(int)
        for f in data.measured_files():
            fp = Path(f).resolve()
            if not fp.is_relative_to(repo):
                continue
            rel = fp.relative_to(repo).as_posix()
            if not rel.startswith(source.rstrip("/") + "/") and rel != source:
                continue
            for ln, ctxs in data.contexts_by_lineno(f).items():
                for c in ctxs:
                    k = test_key(c)
                    if k:
                        lines[rel][ln].add(k)
                        modules[k].add(rel)
                        statements[k] += 1
        cmap.lines = {p: dict(m) for p, m in lines.items()}
        cmap.modules, cmap.statements = dict(modules), dict(statements)
        return cmap
