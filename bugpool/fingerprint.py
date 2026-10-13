"""Form checks: a planted bug must not be findable by its FORMATTING, without reading the code.

Measured (Phase 5 round 3): cosmic-ray's NumberReplacer renders its value as " " + str(val), so a mutated subscript
reads ``candidates[ -1]`` / ``ranges[- 0]`` and a tuple ``( 404, 429)``: a stray space a hunter can spot by eye.

format_gate   (generator) format the file, apply the mutation, format again; if the second format changes anything,
              the mutation introduced non-canonical form -> reject.
check_diff    (plant) exactly one changed line, no whitespace-only hunk, and token spacing identical outside the
              changed tokens -> otherwise raise, loudly, and plant nothing.
"""

from __future__ import annotations

import difflib
import io
import subprocess
import tempfile
import tokenize
from pathlib import Path


class PlantError(ValueError):
    """A pooled diff fails the form checks; it would be findable without reading the code."""


def _ruff_format(ruff: str, text: str, name: str) -> str | None:
    r = subprocess.run([ruff, "format", "--no-cache", "--stdin-filename", name, "-"], input=text, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def format_gate(ruff: str, cosmic_ray: str, path: str, original: str, operator: str, occurrence: int) -> tuple[bool, str]:
    """(passes, detail). Formatting preserves the order of AST nodes, so occurrence N of an operator in the original is
    occurrence N in the formatted file."""
    name = Path(path).name
    base = _ruff_format(ruff, original, name)
    if base is None:
        return False, "the formatter could not parse the file"
    with tempfile.TemporaryDirectory(prefix="bugpool-fmt-") as tmp:
        f = Path(tmp) / name
        f.write_text(base)
        r = subprocess.run([cosmic_ray, "apply", name, operator, str(occurrence)], cwd=tmp, capture_output=True, text=True)
        mutated = f.read_text()
    if r.returncode != 0 or mutated == base:
        return False, "the mutation does not apply to the formatted file"
    again = _ruff_format(ruff, mutated, name)
    if again is None:
        return False, "the formatter could not parse the mutated file"
    if again != mutated:
        changed = [ln for ln in difflib.unified_diff(mutated.splitlines(), again.splitlines(), lineterm="", n=0)
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        return False, "the formatter rewrites the mutated line: " + " | ".join(changed[:2])
    return True, ""


def _tokens(line: str) -> list[tuple[str, str]]:
    """[(token text, whitespace before it)] for one line; tolerant of a line that opens a bracket it does not close."""
    out, prev_end = [], 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(line.lstrip() + "\n").readline):
            if tok.type in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT):
                continue
            s = line.lstrip()
            col = tok.start[1]
            out.append((tok.string, s[prev_end:col]))
            prev_end = tok.end[1]
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def spacing_changes(before: str, after: str) -> list[str]:
    """Places where the mutation altered spacing, not just tokens: around unchanged tokens, or in front of the first
    changed token (``[0]`` -> ``[ -1]``). An inserted keyword's own separating space (``if x`` -> ``if not x``) is fine."""
    a, b = _tokens(before), _tokens(after)
    sm = difflib.SequenceMatcher(a=[t for t, _ in a], b=[t for t, _ in b], autojunk=False)
    problems = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for (ta, wa), (tb, wb) in zip(a[i1:i2], b[j1:j2]):
                if wa != wb and not (i1 == 0 and j1 == 0 and ta == a[0][0]):
                    problems.append(f"spacing before {tb!r} changed ({wa!r} -> {wb!r})")
        elif op == "replace" and a[i1][1] != b[j1][1]:
            problems.append(f"spacing before {b[j1][0]!r} changed ({a[i1][1]!r} -> {b[j1][1]!r})")
    if before.strip() != after.strip() and "".join(before.split()) == "".join(after.split()):
        problems.append("whitespace-only change")
    return problems


def check_diff(diff: str) -> tuple[str, str]:
    """(original line, mutated line) or raise PlantError: exactly one changed line, no whitespace-only hunk, no spacing
    change outside the changed tokens."""
    minus = [ln[1:] for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    plus = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    if len(minus) != 1 or len(plus) != 1:
        raise PlantError(f"the diff changes {len(minus)} line(s) and adds {len(plus)}: exactly one changed line is required")
    problems = spacing_changes(minus[0], plus[0])
    if problems:
        raise PlantError("the planted line would be findable by its form: " + "; ".join(problems))
    return minus[0], plus[0]
