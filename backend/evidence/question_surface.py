"""Structured question input for Phase 4. Deterministic; no model calls.

HARD RULE: an anchor may only reference code the candidate authored. Asking
a candidate about vendored, excluded or someone else's code is the failure
this phase exists to prevent. When authorship cannot be verified from
history (confidence "none"), anchors are emitted only as
kind="unverified_authorship" and say so.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from backend.contracts import Anchor, AuthoredFunction, BugTarget, ClaimCheck, QuestionSurface

from .structure import is_test_path

FUNCTION_KEEP_RATIO = 0.6  # authored_functions: candidate wrote >= 60% of the source lines
ANCHOR_FUNCTION_RATIO = 0.9  # anchors and bug targets: near-entirely the candidate's
BUG_MIN_CCN, BUG_MIN_NLOC, BUG_MIN_RATIO = 3, 8, 0.9
MAX_FUNCTION_ANCHORS, MAX_FILE_ANCHORS, MAX_BUG_TARGETS = 8, 3, 50

# Technologies a README may name, and what would count as evidence for each:
# dependency names (substring match, lowercase), import roots, or file globs.
TECH: dict[str, dict[str, tuple[str, ...]]] = {
    "React": {"deps": ("react",), "imports": ("react",)},
    "Next.js": {"deps": ("next",), "files": ("next.config.*",)},
    "Vue": {"deps": ("vue",)}, "Angular": {"deps": ("@angular/core",)}, "Svelte": {"deps": ("svelte",)},
    "Express": {"deps": ("express",), "imports": ("express",)},
    "Node.js": {"files": ("package.json",)},
    "FastAPI": {"deps": ("fastapi",), "imports": ("fastapi",)},
    "Flask": {"deps": ("flask",), "imports": ("flask",)},
    "Django": {"deps": ("django",), "imports": ("django",), "files": ("manage.py",)},
    "Spring Boot": {"deps": ("spring-boot",), "imports": ("org.springframework",)},
    "PostgreSQL": {"deps": ("pg", "psycopg", "postgres", "asyncpg", "postgresql"), "imports": ("psycopg", "pg")},
    "MySQL": {"deps": ("mysql",), "imports": ("mysql",)},
    "MongoDB": {"deps": ("mongo", "mongoose"), "imports": ("mongoose", "pymongo", "mongodb")},
    "Redis": {"deps": ("redis", "ioredis"), "imports": ("redis", "ioredis")},
    "SQLite": {"deps": ("sqlite",), "imports": ("sqlite3", "sqlite")},
    "Kafka": {"deps": ("kafka",), "imports": ("kafka",)},
    "RabbitMQ": {"deps": ("amqp", "pika", "rabbitmq"), "imports": ("pika", "amqplib")},
    "Celery": {"deps": ("celery",), "imports": ("celery",)},
    "Docker": {"files": ("Dockerfile", "Dockerfile.*", "docker-compose*.yml", "docker-compose*.yaml", "compose.y*ml")},
    "Kubernetes": {"files": ("*.k8s.yaml", "k8s/*", "kubernetes/*", "helm/*", "Chart.yaml")},
    "Terraform": {"files": ("*.tf",)},
    "AWS": {"deps": ("aws-sdk", "@aws-sdk", "boto3", "botocore"), "imports": ("boto3", "aws-sdk", "@aws-sdk")},
    "GraphQL": {"deps": ("graphql", "apollo"), "imports": ("graphql",)},
    "gRPC": {"deps": ("grpc",), "imports": ("grpc",), "files": ("*.proto",)},
    "WebSocket": {"deps": ("ws", "websocket", "socket.io"), "imports": ("ws", "websocket", "socket.io")},
    "Socket.IO": {"deps": ("socket.io",), "imports": ("socket.io",)},
    "TensorFlow": {"deps": ("tensorflow",), "imports": ("tensorflow",)},
    "PyTorch": {"deps": ("torch",), "imports": ("torch",)},
    "scikit-learn": {"deps": ("scikit-learn", "sklearn"), "imports": ("sklearn",)},
    "pandas": {"deps": ("pandas",), "imports": ("pandas",)},
    "NumPy": {"deps": ("numpy",), "imports": ("numpy",)},
    "OpenAI": {"deps": ("openai",), "imports": ("openai",)},
    "LangChain": {"deps": ("langchain",), "imports": ("langchain",)},
    "Tailwind": {"deps": ("tailwindcss",), "files": ("tailwind.config.*",)},
    "Prisma": {"deps": ("prisma", "@prisma/client"), "files": ("prisma/schema.prisma",)},
    "Firebase": {"deps": ("firebase",), "imports": ("firebase",)},
    "Supabase": {"deps": ("supabase",), "imports": ("supabase", "@supabase")},
    "Stripe": {"deps": ("stripe",), "imports": ("stripe",)},
    "Three.js": {"deps": ("three",), "imports": ("three",)},
    "Electron": {"deps": ("electron",)},
    "Nginx": {"files": ("nginx.conf", "*.nginx.conf", "nginx.*.conf", "nginx/*")},
    "JWT": {"deps": ("jsonwebtoken", "jwt", "jose", "pyjwt"), "imports": ("jsonwebtoken", "jwt", "jose")},
    "Monaco": {"deps": ("monaco",), "imports": ("monaco-editor", "@monaco-editor")},
}
_TECH_RE = {name: re.compile(r"(?<![\w.])" + re.escape(name).replace(r"\ ", r"[\s-]?") + r"(?![\w])", re.IGNORECASE)
            for name in TECH}


def _glob(path: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)


def check_claims(snap: dict) -> list[ClaimCheck]:
    """Mechanical: a claim naming a technology is 'unsupported' when no
    dependency, import or config file references it; 'unclear' when it
    names nothing checkable. No guessing about intent."""
    deps = snap.get("dependencies", [])
    imports = snap.get("imports", {})
    paths = snap.get("files_in_scope", [])
    out = []
    for claim in snap.get("readme_claims", []):
        named = [t for t, rx in _TECH_RE.items() if rx.search(claim["text"])]
        if not named:
            out.append(ClaimCheck(claim=claim["text"], verdict="unclear",
                                  evidence_against=["names no technology this check can verify"]))
            continue
        evidence_for, against = [], []
        for tech in named:
            rule = TECH[tech]
            hits = sorted({d["manifest_path"] for d in deps
                           if any(k in d["name"].lower() for k in rule.get("deps", ()))})
            hits += sorted({p for p, mods in imports.items()
                            if any(m.lower().split("/")[0].split(".")[0] in rule.get("imports", ()) or
                                   m.lower().startswith(rule.get("imports", ("\0",))) for m in mods)})[:5]
            hits += sorted({p for p in paths if any(_glob(p, g) for g in rule.get("files", ()))})[:5]
            if hits:
                evidence_for += [f"{tech}: {h}" for h in hits[:6]]
            else:
                against.append(f"no dependency manifest, import, or config file references {tech}")
        out.append(ClaimCheck(claim=claim["text"], technologies=named, evidence_for=evidence_for,
                              evidence_against=against, verdict="unsupported" if against else "supported"))
    return out


def _ratio(lines, start: int, end: int) -> float | None:
    window = lines[start - 1:end]
    src = [authored for is_src, authored in window if is_src]
    return (sum(src) / len(src)) if src else None


def build(snap: dict, line_authorship: dict | None, confidence: str | None, reason: str | None) -> QuestionSurface:
    structure = snap.get("structure") or {}
    gaps = []
    if not structure.get("test_dirs") and not structure.get("test_files"):
        gaps.append("no test directory or test files found")
    if not structure.get("ci_files"):
        gaps.append("no CI configuration found")
    if not snap.get("manifests"):
        gaps.append("no dependency manifest found")
    surface = QuestionSurface(claim_vs_code=check_claims(snap), coverage_gaps=gaps)
    if line_authorship is None:
        return surface  # no identity: nothing can be attributed, so no anchors

    authored: list[AuthoredFunction] = []
    for fn in snap.get("functions", []):
        lines = line_authorship.get(fn["path"])
        if not lines:
            continue
        r = _ratio(lines, fn["start_line"], fn["end_line"])
        if r is None or r < FUNCTION_KEEP_RATIO:
            continue
        authored.append(AuthoredFunction(path=fn["path"], name=fn["name"], start_line=fn["start_line"],
                                         end_line=fn["end_line"], ccn=fn["ccn"], nloc=fn["nloc"], params=fn["params"],
                                         authored_line_ratio=round(r, 4), lizard_start_line=fn["lizard_start_line"],
                                         lizard_end_line=fn["lizard_end_line"], boundaries_agree=fn["boundaries_agree"]))
    surface.authored_functions = authored
    surface.boundary_disagreements = [f for f in authored if f.boundaries_agree is False]

    targets = []
    for f in authored:
        problems = []
        if f.ccn is None or f.ccn < BUG_MIN_CCN:
            problems.append(f"ccn {f.ccn} < {BUG_MIN_CCN}")
        if f.nloc is None or f.nloc < BUG_MIN_NLOC:
            problems.append(f"nloc {f.nloc} < {BUG_MIN_NLOC}")
        if f.authored_line_ratio < BUG_MIN_RATIO:
            problems.append(f"authored {f.authored_line_ratio:.0%} < {BUG_MIN_RATIO:.0%}")
        if is_test_path(f.path):
            problems.append("in a test directory")
        if f.lizard_end_line is not None and f.lizard_end_line != f.end_line:
            # lizard misreads some syntax (JSX): its ccn/nloc then cover the wrong span
            problems.append(f"lizard ends at line {f.lizard_end_line}, tree-sitter at {f.end_line}: "
                            "complexity metrics unreliable")
        if confidence == "none":
            problems.append("authorship unverifiable from history")
        eligible = not problems
        targets.append(BugTarget(**f.model_dump(), eligible=eligible,
                                 reason="meets every criterion" if eligible else "; ".join(problems)))
    surface.bug_targets = sorted(targets, key=lambda t: (not t.eligible, -(t.ccn or 0), t.path))[:MAX_BUG_TARGETS]

    strong = sorted((f for f in authored if f.authored_line_ratio >= ANCHOR_FUNCTION_RATIO),
                    key=lambda f: (-(f.ccn or 0), -(f.nloc or 0), f.path))[:MAX_FUNCTION_ANCHORS]
    hot = sorted(((p, sum(a for s, a in lines if s)) for p, lines in line_authorship.items()),
                 key=lambda x: -x[1])[:MAX_FILE_ANCHORS]
    anchors: list[Anchor] = []
    if confidence == "none":
        for f in strong:
            anchors.append(Anchor(kind="unverified_authorship", path=f.path, start_line=f.start_line,
                                  end_line=f.end_line, why=f"Function {f.name} is attributed to the candidate, but "
                                  f"{reason} Do not treat it as their own work without other evidence."))
    else:
        for f in strong:
            anchors.append(Anchor(kind="authored_function", path=f.path, start_line=f.start_line, end_line=f.end_line,
                                  why=f"The candidate wrote {f.authored_line_ratio:.0%} of {f.name}'s source lines; "
                                      f"cyclomatic complexity {f.ccn}, {f.nloc} lines."))
        for path, n in hot:
            if n <= 0:
                continue
            lines = line_authorship[path]
            best = _longest_authored_range(lines)
            if best:
                anchors.append(Anchor(kind="hot_file", path=path, start_line=best[0], end_line=best[1],
                                      why=f"{n} source lines in this file are the candidate's; lines "
                                          f"{best[0]}-{best[1]} are the longest run they wrote."))
        for ep in structure.get("entry_points", []):
            lines = line_authorship.get(ep)
            r = _ratio(lines, 1, len(lines)) if lines else None
            if r is not None and r >= ANCHOR_FUNCTION_RATIO:
                anchors.append(Anchor(kind="entry_point", path=ep, start_line=1, end_line=len(lines),
                                      why=f"Entry point; the candidate wrote {r:.0%} of its source lines."))
    surface.anchors = anchors
    return surface


def _longest_authored_range(lines: list[tuple[bool, bool]]) -> tuple[int, int] | None:
    """Longest run of candidate-authored source lines, bridging non-source lines only."""
    best = cur = None
    for i, (is_src, mine) in enumerate(lines, start=1):
        if is_src and mine:
            cur = (cur[0], i) if cur else (i, i)
        elif is_src and not mine:
            cur = None
        if cur and (best is None or (cur[1] - cur[0]) > (best[1] - best[0])):
            best = cur
    return best
