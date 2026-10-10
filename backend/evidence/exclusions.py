"""Which repository files count toward authorship (Phase 3.5).

Path rules are NOT our own lists. They are GitHub Linguist's ``vendor.yml``
and ``documentation.yml``, vendored unmodified under ``linguist/`` and
pinned to one upstream commit (see THIRD_PARTY.md). Every pattern is
compiled with Python's ``re`` and matched with ``.search()`` against the
repo-relative POSIX path; a pattern that fails to compile is a hard error,
never silently dropped.

On top of the paths, exactly two content heuristics from Linguist's
``generated.rb`` (minified files and source-map references) and the
repository's own ``.gitattributes`` overrides (linguist-vendored,
linguist-generated, linguist-documentation, with ``-attr`` / ``!attr`` /
``attr=false`` to unmark) are applied. Everything else (lock files,
"do not edit" headers, duplicates, binaries, unparseable files) is decided
by pygount's analysis in ``counting.py``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

LINGUIST_DIR = Path(__file__).resolve().parent / "linguist"

# Reasons a file is excluded. Keys of RepoInfo.excluded_*_by_reason.
VENDORED_PATH = "vendored_path"
DOCUMENTATION_PATH = "documentation_path"
GENERATED = "generated"  # .gitattributes linguist-generated, or pygount's own generated detection
MINIFIED = "minified"
SOURCE_MAP = "source_map"
DUPLICATE = "duplicate"
UNPARSEABLE = "unparseable"  # pygount error/unknown, or per-line counts that do not reconcile
BINARY = "binary"
EMPTY = "empty"
TOO_LARGE = "too_large"  # over a size or file-count cap
REASONS = (VENDORED_PATH, DOCUMENTATION_PATH, GENERATED, MINIFIED, SOURCE_MAP, DUPLICATE, UNPARSEABLE,
           BINARY, EMPTY, TOO_LARGE)


class LinguistPatternError(RuntimeError):
    """A vendored Linguist pattern failed to compile. Never dropped silently."""


def load_patterns(name: str, directory: Path = LINGUIST_DIR) -> list[re.Pattern]:
    patterns = yaml.safe_load((directory / name).read_text(encoding="utf-8"))
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise LinguistPatternError(f"{name}: expected a YAML list of regex strings")
    compiled, failures = [], []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            failures.append(f"{pattern!r}: {exc}")
    if failures:
        raise LinguistPatternError(f"{name}: {len(failures)} pattern(s) failed to compile: " + "; ".join(failures))
    return compiled


VENDOR_PATTERNS = load_patterns("vendor.yml")
DOCUMENTATION_PATTERNS = load_patterns("documentation.yml")


def linguist_path_reason(path: str) -> str | None:
    """vendored_path / documentation_path if a Linguist pattern matches the
    repo-relative POSIX path (vendor checked first, as Linguist does)."""
    if any(p.search(path) for p in VENDOR_PATTERNS):
        return VENDORED_PATH
    if any(p.search(path) for p in DOCUMENTATION_PATTERNS):
        return DOCUMENTATION_PATH
    return None


# --------------------------------------------------------------------------
# the two content heuristics from Linguist's generated.rb
# --------------------------------------------------------------------------

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")
_SOURCE_MAP_RE = re.compile(r"^/[*/][#@] source(?:Mapping)?URL|sourceURL=")
_MIN_EXTS = (".js", ".css")


def _lines(text: str) -> list[str]:
    # Like Ruby's data.split(newline, -1): a trailing newline yields a final "".
    return _LINE_SPLIT.split(text)


def is_minified(path: str, text: str) -> bool:
    """generated.rb ``minified_files?``: a .js/.css file whose average line
    length exceeds 110 characters (integer division, as in the Ruby source)."""
    if PurePosixPath(path).suffix.lower() not in _MIN_EXTS:
        return False
    lines = _lines(text)
    if not lines:
        return False
    return sum(len(line) for line in lines) // len(lines) > 110


def has_source_map(path: str, text: str) -> bool:
    """generated.rb ``has_source_map?``: a .js/.css file whose last two lines
    reference a source map (``//# sourceMappingURL=...`` and variants)."""
    if PurePosixPath(path).suffix.lower() not in _MIN_EXTS:
        return False
    return any(_SOURCE_MAP_RE.match(line) for line in _lines(text)[-2:])


# --------------------------------------------------------------------------
# .gitattributes overrides
# --------------------------------------------------------------------------

LINGUIST_ATTRS = {"linguist-vendored": VENDORED_PATH, "linguist-generated": GENERATED,
                  "linguist-documentation": DOCUMENTATION_PATH}


@dataclass
class AttrRule:
    pattern: str
    attrs: dict[str, bool]


@dataclass
class GitAttributes:
    rules: list[AttrRule] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "GitAttributes":
        rules = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern, attrs = parts[0], {}
            for tok in parts[1:]:
                if tok.startswith(("-", "!")):
                    name, val = tok[1:], False
                elif "=" in tok:
                    name, v = tok.split("=", 1)
                    val = v.strip().lower() not in ("false", "0", "no", "unset")
                else:
                    name, val = tok, True
                if name in LINGUIST_ATTRS:
                    attrs[name] = val
            if attrs:
                rules.append(AttrRule(pattern, attrs))
        return cls(rules)

    def overrides(self, path: str) -> dict[str, bool]:
        """Final linguist-* attribute values for ``path`` (later lines win)."""
        out: dict[str, bool] = {}
        for rule in self.rules:
            if _attr_match(rule.pattern, path):
                out.update(rule.attrs)
        return out


def _attr_match(pattern: str, path: str) -> bool:
    """gitattributes-style matching: patterns without '/' match the basename
    at any depth; patterns with '/' are anchored to the repo root; '**' spans dirs."""
    pat = pattern.lstrip("/")
    if pat.endswith("/"):
        pat += "**"
    if "/" not in pattern.rstrip("/"):
        return fnmatch.fnmatchcase(PurePosixPath(path).name, pat) or fnmatch.fnmatchcase(path, pat)
    if fnmatch.fnmatchcase(path, pat):
        return True
    if pat.endswith("/**"):
        return path.startswith(pat[:-3] + "/")
    return False


def path_reason(path: str, attrs: GitAttributes | None = None) -> str | None:
    """Exclusion reason from the path alone (Linguist patterns, then the
    repository's .gitattributes overrides), or None if the path may count."""
    reason = linguist_path_reason(path)
    if attrs is None:
        return reason
    ov = attrs.overrides(path)
    for attr, why in LINGUIST_ATTRS.items():  # explicit marking wins
        if ov.get(attr) is True:
            return why
    for attr, why in LINGUIST_ATTRS.items():  # explicit unmarking restores the file
        if ov.get(attr) is False and reason == why:
            return None
    return reason


def content_reason(path: str, text: str) -> str | None:
    """minified / source_map from the two generated.rb content heuristics."""
    if is_minified(path, text):
        return MINIFIED
    if has_source_map(path, text):
        return SOURCE_MAP
    return None
