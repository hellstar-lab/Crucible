"""Technology -> artifact lexicon (data/tech_artifacts.json) and recognition of technologies in claim text."""

from __future__ import annotations

import fnmatch
import json
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

DATA = Path(__file__).resolve().parent / "data" / "tech_artifacts.json"
CONCEPTS = Path(__file__).resolve().parent / "data" / "code_concepts.json"
# Aliases that are also ordinary words (or very short) only match with their exact capitalisation.
_CASE_SENSITIVE_MAX_LEN = 4
_COMMON_WORDS = frozenset({"go", "express", "lambda", "vault", "consul", "react", "rust", "node", "rest", "c", "s3", "elk", "unix",
                           "linux", "html", "sass", "koa", "github"})


@lru_cache(maxsize=1)
def technologies() -> dict[str, dict]:
    return json.loads(DATA.read_text(encoding="utf-8"))["technologies"]


def _alias_pattern(alias: str) -> re.Pattern:
    sensitive = len(alias) <= _CASE_SENSITIVE_MAX_LEN or alias.lower() in _COMMON_WORDS
    body = re.escape(alias).replace(r"\ ", r"[\s-]")
    return re.compile(rf"(?<![\w.+#/-]){body}(?![\w+#-])", 0 if sensitive else re.IGNORECASE)


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[str, re.Pattern]]:
    out = []
    for key, entry in technologies().items():
        for alias in sorted(set(entry["aliases"]), key=len, reverse=True):
            out.append((key, _alias_pattern(alias)))
    return out


def recognise(text: str) -> list[str]:
    """Lexicon keys of the technologies a claim names, in order of first appearance."""
    hits: dict[str, int] = {}
    for key, rx in _patterns():
        m = rx.search(text or "")
        if m and (key not in hits or m.start() < hits[key]):
            hits[key] = m.start()
    # "ELK" is a stack: also count its parts when written out; keep the order of appearance
    return sorted(hits, key=hits.get)


def file_pattern_match(path: str, pattern) -> bool:
    """A string pattern is a glob on the file name or the full path; a trailing '/' matches a directory anywhere."""
    if isinstance(pattern, dict):
        return file_pattern_match(path, pattern["glob"])
    p = PurePosixPath(path)
    if pattern.endswith("/"):
        d = pattern.rstrip("/")
        return d in p.parts[:-1] or path.startswith(d + "/") or ("/" in d and ("/" + d + "/") in ("/" + path))
    return fnmatch.fnmatchcase(p.name, pattern) or fnmatch.fnmatchcase(path, pattern)


def import_matches(module: str, prefixes: list[str]) -> str | None:
    """The lexicon import prefix a module path matches (exact, or followed by '.', '/' or ':'), or None."""
    for pre in prefixes:
        if module == pre or module.startswith((pre + ".", pre + "/", pre + ":")):
            return pre
    return None


def _concept_alias(alias: str) -> re.Pattern:
    """Whole words, case-insensitive; a space in the alias matches a space or a hyphen ("row level" ~ "row-level")."""
    body = re.escape(alias).replace(r"\ ", r"[\s-]")
    return re.compile(r"(?i)(?<![\w-])" + body + r"(?![\w-])")


@lru_cache(maxsize=1)
def concepts() -> dict[str, dict]:
    """Stage 2: code concepts (thread pool, semaphore, row lock ...) -> claim aliases and code patterns."""
    data = json.loads(CONCEPTS.read_text(encoding="utf-8"))["concepts"]
    for entry in data.values():
        entry["_aliases"] = [_concept_alias(a) for a in entry["aliases"]]
        entry["_patterns"] = [re.compile(p) for p in entry["patterns"]]
    return data


def recognise_concepts(text: str) -> list[str]:
    """Concept keys a claim names, in order of first appearance."""
    hits: dict[str, int] = {}
    for key, entry in concepts().items():
        for rx in entry["_aliases"]:
            m = rx.search(text or "")
            if m and (key not in hits or m.start() < hits[key]):
                hits[key] = m.start()
    return sorted(hits, key=hits.get)
