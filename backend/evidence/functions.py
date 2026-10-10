"""Function boundaries (tree-sitter) and complexity (lizard).

tree-sitter gives the syntactic node of every function; lizard gives
cyclomatic complexity, NLOC and parameter count. Each tree-sitter function is
matched to a lizard function and the two line ranges are compared: the
boundary used is always tree-sitter's (the whole syntactic node), and any
disagreement is recorded explicitly, never trusted silently. Example: a C
function whose return type sits on its own line starts one line earlier in
tree-sitter than in lizard.

tree-sitter-language-pack is pinned to 0.13.0, the last release whose wheel
bundles compiled grammars. 1.x downloads grammars (native libraries) at
runtime, which would make analysis depend on the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

import lizard
from tree_sitter_language_pack import get_parser

log = logging.getLogger(__name__)

# extension -> (tree-sitter language, function node types)
_JS_FUNCS = {"function_declaration", "method_definition", "arrow_function", "function_expression",
             "generator_function_declaration"}
LANGUAGES: dict[str, tuple[str, set[str]]] = {
    ".py": ("python", {"function_definition"}),
    ".js": ("javascript", _JS_FUNCS), ".jsx": ("javascript", _JS_FUNCS), ".mjs": ("javascript", _JS_FUNCS),
    ".cjs": ("javascript", _JS_FUNCS),
    ".ts": ("typescript", _JS_FUNCS), ".tsx": ("tsx", _JS_FUNCS),
    ".java": ("java", {"method_declaration", "constructor_declaration"}),
    ".c": ("c", {"function_definition"}), ".h": ("c", {"function_definition"}),
    ".cpp": ("cpp", {"function_definition"}), ".cc": ("cpp", {"function_definition"}),
    ".cxx": ("cpp", {"function_definition"}), ".hpp": ("cpp", {"function_definition"}),
    ".go": ("go", {"function_declaration", "method_declaration"}),
    ".rs": ("rust", {"function_item"}),
    ".rb": ("ruby", {"method", "singleton_method"}),
    ".php": ("php", {"function_definition", "method_declaration"}),
    ".kt": ("kotlin", {"function_declaration"}),
    ".cs": ("csharp", {"method_declaration", "constructor_declaration"}),
}
_NAME_NODE_TYPES = {"identifier", "field_identifier", "property_identifier", "qualified_identifier",
                    "simple_identifier", "constant", "name", "destructor_name", "operator_name"}


@dataclass
class FunctionSpan:
    path: str
    name: str
    start_line: int
    end_line: int
    ccn: int | None = None
    nloc: int | None = None
    params: int | None = None
    lizard_start_line: int | None = None
    lizard_end_line: int | None = None

    @property
    def boundaries_agree(self) -> bool | None:
        if self.lizard_start_line is None:
            return None
        return (self.lizard_start_line, self.lizard_end_line) == (self.start_line, self.end_line)


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _name(node, src: bytes) -> str:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named, src)
    decl = node.child_by_field_name("declarator")  # C / C++: dig through declarators
    while decl is not None:
        if decl.type in _NAME_NODE_TYPES:
            return _text(decl, src)
        inner = decl.child_by_field_name("declarator")
        if inner is None:
            for child in decl.children:
                if child.type in _NAME_NODE_TYPES:
                    return _text(child, src)
        decl = inner
    parent = node.parent  # anonymous JS/TS functions: const x = () => {} / {x: function () {}}
    if parent is not None:
        for field in ("name", "key", "left"):
            target = parent.child_by_field_name(field)
            if target is not None and target is not node:
                return _text(target, src)
    return "<anonymous>"


def tree_sitter_functions(path: str, src: bytes) -> list[FunctionSpan]:
    spec = LANGUAGES.get(PurePosixPath(path).suffix.lower())
    if spec is None:
        return []
    lang, kinds = spec
    try:
        tree = get_parser(lang).parse(src)
    except Exception as exc:  # noqa: BLE001 - parser failure on untrusted input
        log.warning("tree-sitter could not parse %s: %s", path, exc)
        return []
    out: list[FunctionSpan] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in kinds:
            out.append(FunctionSpan(path, _name(node, src), node.start_point[0] + 1, node.end_point[0] + 1))
        stack.extend(reversed(node.children))
    return sorted(out, key=lambda f: (f.start_line, f.end_line))


def _short(name: str) -> str:
    for sep in ("::", "."):
        name = name.rsplit(sep, 1)[-1]
    return name


def attach_lizard(path: str, src: bytes, spans: list[FunctionSpan]) -> list[FunctionSpan]:
    """Fill ccn/nloc/params from lizard by matching name and overlapping range."""
    try:
        info = lizard.analyze_file.analyze_source_code(path, src.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("lizard could not analyse %s: %s", path, exc)
        return spans
    candidates = list(info.function_list)
    for span in spans:
        best, best_cost = None, None
        for f in candidates:
            if f.end_line < span.start_line or f.start_line > span.end_line:
                continue  # no overlap
            name_ok = _short(f.name) == _short(span.name) or span.name == "<anonymous>"
            cost = abs(f.start_line - span.start_line) + abs(f.end_line - span.end_line) + (0 if name_ok else 1000)
            if best_cost is None or cost < best_cost:
                best, best_cost = f, cost
        if best is not None and best_cost is not None and best_cost < 1000:
            span.ccn, span.nloc = best.cyclomatic_complexity, best.nloc
            span.params = getattr(best, "parameter_count", len(getattr(best, "parameters", []) or []))
            span.lizard_start_line, span.lizard_end_line = best.start_line, best.end_line
    return spans


def extract_functions(path: str, src: bytes) -> list[FunctionSpan]:
    return attach_lizard(path, src, tree_sitter_functions(path, src))
