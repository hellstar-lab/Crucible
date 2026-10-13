"""The docs telegraph check: does the repository's PROSE describe what the mutated line means?

For each candidate: search every tracked markdown file and every docstring for a passage (a sentence or a list item)
that names the enclosing function (a function's own docstring names it implicitly), or an identifier on the mutated
line, TOGETHER WITH the meaning of the original operator (``<=`` -> "at most", "no more than", ...; an integer literal -> the number). Such a passage tells a reader
what the line should say, so the bug can be found by reading the docs against the code.

Recorded per candidate, never used to reject: the hunt may legitimately be easier with the docs, and the session's
two conditions ("code" hides docs/, "docs" shows it) measure exactly that.
"""

from __future__ import annotations

import ast
import builtins
import keyword
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .validate import GENERIC, enclosing_function

MAX_HITS = 6
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
# the meaning of the ORIGINAL token, the thing a passage would describe
MEANING: dict[str, list[str]] = {
    "Eq": ["==", "equal", "equals", "same as", "matches", "exactly"],
    "NotEq": ["!=", "not equal", "differs", "different", "other than", "mismatch"],
    "Lt": ["<", "less than", "fewer than", "below", "under", "smaller than", "before"],
    "LtE": ["<=", "≤", "at most", "no more than", "up to", "not exceed", "or fewer", "or less"],
    "Gt": [">", "greater than", "more than", "above", "over", "exceeds", "larger than", "after"],
    "GtE": [">=", "≥", "at least", "no fewer than", "or more", "minimum"],
    "Is": ["is none", "none", "null", "missing", "not found", "absent", "does not exist", "doesn't exist", "no row"],
    "IsNot": ["is not none", "exists", "found", "present", "already"],
    "True": ["true", "enabled"],
    "False": ["false", "disabled"],
    "Not": ["not", "no", "unless", "without", "never", "isn't", "missing"],
    "Cond": ["if", "when", "only", "whether"],  # AddNot: the original is the bare condition
    "And": ["both", "all", "every", "as well as", "together"],
    "Or": ["either", "or", "any", "one of", "whichever"],
    "Break": ["stop", "stops", "break", "exit the loop", "first", "until"],
    "Continue": ["skip", "skips", "continue", "ignore", "next"],
}
_SYMBOL = re.compile(r"^[<>=!≤≥]+$")


@dataclass
class Passage:
    source: str  # path:line
    kind: str  # markdown | docstring
    text: str
    owner: str | None = None  # a docstring's function or class: its own docstring names it implicitly


def original_meaning(operator: str, original_line: str) -> tuple[str, list[str]]:
    """(label, phrases) for what the ORIGINAL code at the mutation site means."""
    name = operator.split("/")[-1]
    m = re.fullmatch(r"ReplaceComparisonOperator_(\w+)_(\w+)", name)
    if m:
        return m.group(1), MEANING[m.group(1)]
    if name == "NumberReplacer":
        nums = sorted(set(re.findall(r"(?<![\w.])\d+(?![\w.])", original_line)), key=int)
        phrases = nums + [_WORDS[int(n)] for n in nums if int(n) < len(_WORDS)]
        return "integer " + "/".join(nums), phrases
    key = {"ReplaceTrueWithFalse": "True", "ReplaceFalseWithTrue": "False", "AddNot": "Cond",
           "ReplaceUnaryOperator_Delete_Not": "Not", "ReplaceAndWithOr": "And", "ReplaceOrWithAnd": "Or",
           "ReplaceBreakWithContinue": "Break", "ReplaceContinueWithBreak": "Continue"}.get(name)
    return (key, MEANING[key]) if key else (name, [])


def _phrase_rx(phrase: str) -> re.Pattern:
    if _SYMBOL.match(phrase):
        return re.compile(rf"(?<![<>=!-]){re.escape(phrase)}(?![<>=])")
    return re.compile(rf"(?i)(?<![\w]){re.escape(phrase)}(?![\w])")


def names_at(repo: Path, path: str, line: int, text: str) -> list[str]:
    """The enclosing function and the identifiers on the mutated line (not keywords, builtins or generic words)."""
    skip = set(keyword.kwlist) | set(dir(builtins)) | GENERIC
    idents = [i for i in dict.fromkeys(re.findall(r"[A-Za-z_]\w*", text)) if len(i) >= 3 and i.lower() not in skip]
    func = enclosing_function(repo, path, line)
    return ([func] if func and func not in idents else []) + idents


def _name_rx(name: str) -> re.Pattern:
    alts = [re.escape(name)]
    words = [w for w in name.strip("_").split("_") if w]
    if len(words) >= 2:  # available_seats -> "available seats" / "available-seats"
        alts.append(r"[\s-]+".join(map(re.escape, words)))
    return re.compile(rf"(?i)(?<![\w])(?:{'|'.join(alts)})(?![\w])")


def _sentences(block: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`(])", block) if s.strip()]


def corpus(repo: Path) -> list[Passage]:
    """Every sentence / list item of tracked markdown, and every sentence of every docstring in tracked Python."""
    files = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split("\n")
    out: list[Passage] = []
    for rel in files:
        p = Path(repo) / rel
        if rel.endswith((".md", ".markdown", ".rst", ".txt")):
            try:
                lines = p.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            block, start = [], 1
            for i, ln in enumerate(lines + [""], 1):
                item = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", ln)
                if not ln.strip() or item or ln.startswith("#"):
                    if block:
                        out += [Passage(f"{rel}:{start}", "markdown", s) for s in _sentences(" ".join(block))]
                    block, start = ([ln.strip()] if (item or ln.startswith("#")) and ln.strip() else []), i
                else:
                    block.append(ln.strip())
                    if len(block) == 1:
                        start = i
        elif rel.endswith(".py"):
            try:
                tree = ast.parse(p.read_text())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node)
                    if doc:
                        line, owner = node.body[0].lineno, getattr(node, "name", None)
                        out += [Passage(f"{rel}:{line}", "docstring", s, owner) for s in _sentences(" ".join(doc.split()))]
    return out


def check(repo: Path, cand: dict, passages: list[Passage], hidden_dirs: tuple[str, ...] = ("docs",)) -> dict:
    """{described, hits, ...} for one pooled candidate. ``hidden_dirs``: where the "code" condition hides prose."""
    label, phrases = original_meaning(cand["operator"], cand["original_line"])
    names = names_at(repo, cand["path"], cand["line"], cand["original_line"])
    prx = [(ph, _phrase_rx(ph)) for ph in phrases]
    nrx = [(n, _name_rx(n)) for n in names]
    hits = []
    for ps in passages:
        n = next((n for n, rx in nrx if n == ps.owner or rx.search(ps.text)), None)
        if n is None:
            continue
        ph = next((ph for ph, rx in prx if rx.search(ps.text)), None)
        if ph is None:
            continue
        hits.append({"source": ps.source, "kind": ps.kind, "name": n, "meaning": ph, "excerpt": ps.text[:240]})
    hidden = lambda h: any(h["source"].startswith(d.rstrip("/") + "/") for d in hidden_dirs)  # noqa: E731
    hits.sort(key=hidden)  # prose visible in both conditions first
    in_hidden = [h for h in hits if hidden(h)]
    return {"described": bool(hits), "original_meaning": label, "names": names, "phrases": phrases,
            "hits_total": len(hits), "hits_in_hidden_dirs": len(in_hidden), "hits_visible_in_both": len(hits) - len(in_hidden),
            "hidden_dirs": list(hidden_dirs), "hits": hits[:MAX_HITS],
            "rule": "recorded, never used to reject"}
