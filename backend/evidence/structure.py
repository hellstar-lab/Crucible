"""Repository structure facts from file paths alone. No content, no identity."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from backend.contracts import Structure

TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "integration_tests"}
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx",
                      "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*_spec.rb", "*Test.java", "*Tests.java",
                      "*Test.kt", "*Tests.swift", "*_test.rs", "*_test.exs", "*Test.php", "*Tests.cs")
ENTRY_POINT_NAMES = {"main.py", "__main__.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "server.py", "cli.py",
                     "index.js", "server.js", "app.js", "main.js", "index.ts", "server.ts", "main.ts", "app.ts",
                     "main.go", "main.rs", "Main.java", "Application.java", "Program.cs", "main.c", "main.cpp",
                     "index.php", "main.kt", "Main.kt", "App.tsx", "App.jsx", "main.tsx", "main.jsx"}
CI_PATTERNS = (".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".circleci/config.yml",
               "Jenkinsfile", "azure-pipelines.yml", ".travis.yml", "bitbucket-pipelines.yml", ".drone.yml",
               "buildkite.yml", ".buildkite/*.yml", "appveyor.yml", ".woodpecker.yml")
CONTAINER_PATTERNS = ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                      "docker-compose.*.yml", "docker-compose.*.yaml", "compose.yml", "compose.yaml", "Containerfile",
                      ".dockerignore", "skaffold.yaml", "Chart.yaml")
MIGRATION_DIR_NAMES = {"migrations", "migration", "alembic", "migrate", "flyway", "liquibase"}
CONFIG_PATTERNS = ("pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", ".flake8", "mypy.ini",
                   "tsconfig.json", "tsconfig.*.json", "jsconfig.json", ".eslintrc*", "eslint.config.*", ".prettierrc*",
                   "webpack.config.*", "vite.config.*", "next.config.*", "babel.config.*", ".babelrc", "rollup.config.*",
                   "tailwind.config.*", "postcss.config.*", "jest.config.*", "vitest.config.*", "Makefile",
                   "CMakeLists.txt", ".env.example", ".env.sample", "application.properties", "application.yml",
                   "application.yaml", "nginx.conf", "*.nginx.conf", "nginx.*.conf", "Procfile", "vercel.json",
                   "netlify.toml", "fly.toml", "render.yaml", "app.json", "serverless.yml", ".editorconfig")


def _match(path: str, patterns) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or ("/" not in p and fnmatch.fnmatchcase(name, p)) for p in patterns)


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in TEST_DIR_NAMES for part in p.parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(p.name, pat) for pat in TEST_FILE_PATTERNS)


def detect_structure(paths: list[str]) -> Structure:
    """Classify a repository's paths (repo-relative, already scope-filtered)."""
    top = sorted({PurePosixPath(p).parts[0] + "/" for p in paths if len(PurePosixPath(p).parts) > 1})
    test_dirs: set[str] = set()
    migration_dirs: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        for i, part in enumerate(parts[:-1]):
            if part in TEST_DIR_NAMES:
                test_dirs.add("/".join(parts[: i + 1]) + "/")
                break
        for i, part in enumerate(parts[:-1]):
            if part in MIGRATION_DIR_NAMES or (part == "migrate" and i > 0 and parts[i - 1] == "db"):
                migration_dirs.add("/".join(parts[: i + 1]) + "/")
                break
    return Structure(
        top_level_dirs=top,
        entry_points=sorted(p for p in paths if PurePosixPath(p).name in ENTRY_POINT_NAMES),
        test_dirs=sorted(test_dirs),
        test_files=sorted(p for p in paths if is_test_path(p))[:200],
        ci_files=sorted(p for p in paths if _match(p, CI_PATTERNS)),
        container_files=sorted(p for p in paths if _match(p, CONTAINER_PATTERNS)),
        migration_dirs=sorted(migration_dirs),
        config_files=sorted(p for p in paths if _match(p, CONFIG_PATTERNS))[:100],
    )
