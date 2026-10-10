"""Phase 4B caps, cutoffs and fixed vocabularies. Every value here feeds retrieval_version()."""

from __future__ import annotations

import hashlib
from pathlib import Path

RETRIEVAL_VERSION_BASE = "4B.1"
MAX_ITEMS = 10
EXCERPT_MAX_CHARS = 400
DEP_FUZZY_CUTOFF = 90  # rapidfuzz fuzz.ratio, dependency name vs lexicon package name (pinned; recorded per item)
OWNERSHIP_FUZZY_CUTOFF = 85  # rapidfuzz fuzz.ratio, claim artifact word vs path / function-name token
OWNERSHIP_MIN_WORD = 4  # claim words shorter than this never drive an ownership match
MAX_TEXT_BYTES = 512_000  # files larger than this are not read for config keys or excerpts

# Snapshot exclusion: files the 3.5 rules excluded are never evidence; neither are pygount's non-source states below.
UNUSABLE_SKIP_STATES = frozenset({"generated", "binary", "duplicate"})

# r_config: config keys are only searched in these kinds of files (by suffix or exact name)
CONFIG_SUFFIXES = frozenset({".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env", ".tf", ".tfvars", ".hcl",
                             ".properties", ".xml", ".gradle"})
CONFIG_NAMES = frozenset({"Dockerfile", "Procfile", "Jenkinsfile", "Vagrantfile", ".env.example", "Makefile"})

# r_structure: claim words -> repo_facts.structure keys
STRUCTURE_WORDS: dict[str, tuple[str, ...]] = {
    "test_dirs": ("test", "tests", "testing", "unit", "integration", "e2e", "coverage", "pytest", "jest", "spec"),
    "test_files": ("test", "tests", "testing", "unit", "integration", "e2e", "coverage", "pytest", "jest", "spec"),
    "ci_files": ("ci", "cd", "ci/cd", "pipeline", "pipelines", "github actions", "actions", "circleci", "jenkins", "gitlab"),
    "container_files": ("docker", "dockerfile", "container", "containers", "containerized", "containerised", "compose"),
    "migration_dirs": ("migration", "migrations", "alembic", "flyway", "liquibase", "schema"),
    "config_files": ("config", "configuration", "linting", "lint", "pre-commit", "formatter", "mypy", "flake8"),
}

# r_benchmark: directories and file patterns that hold benchmarks or load tests
BENCHMARK_DIRS = frozenset({"bench", "benches", "benchmark", "benchmarks", "perf", "performance", "load_test", "load_tests",
                            "loadtest", "loadtests", "load-test", "load-tests", "k6", "locust", "jmh", "gatling", "stress"})
BENCHMARK_GLOBS = ("*bench*.py", "*benchmark*", "*_bench.go", "*.bench.js", "*.bench.ts", "locustfile.py", "*.k6.js", "k6*.js",
                   "artillery*.yml", "artillery*.yaml", "wrk*.lua", "*Benchmark*.java", "*.jmh.*")
BENCHMARK_CONTENT = (("*_test.go", "func Benchmark"), ("*.py", "pytest_benchmark"), ("*.py", "benchmark("))

SKIPPED_KINDS = {
    "role_or_team": "role_or_team is not knowable from code: titles, team size and leadership leave no repository artifact",
    "outcome": "outcome is not knowable from code: business impact (revenue, users, cost) is never recorded in a repository",
    "other": "other: no checkability rule matched this claim, so retrieval does not guess at a search",
}

KIND_RULES: dict[str, tuple[str, ...]] = {
    "technology_use": ("r_dependency", "r_import", "r_config"),
    "ownership": ("r_ownership",),
    "artifact_exists": ("r_structure", "r_config"),
    "scale": ("r_scale",),
    "timeline": ("r_history",),
    "metric": ("r_benchmark",),
}

_HERE = Path(__file__).resolve().parent


def retrieval_version() -> str:
    """Changes whenever a rule, the lexicon or a cutoff changes, so a cached EvidenceSet invalidates."""
    h = hashlib.sha256()
    for name in ("config.py", "rules.py", "rank.py", "lexicon.py", "sources.py", "data/tech_artifacts.json"):
        h.update((_HERE / name).read_bytes())
    return f"{RETRIEVAL_VERSION_BASE}+{h.hexdigest()[:10]}"
