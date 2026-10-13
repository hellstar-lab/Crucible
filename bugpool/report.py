"""The session report: a session record rendered as one self-contained page, in the style of the Phase 4 claim
evidence report (it reuses that page's CSS and its traceability check: every number on the page is a field of the
record, formatted, never calculated, and the render fails otherwise).

Shows: the candidate's module and operator; the found / not-found outcome and the LOCALISATION COST for the participant
and for the stranger on the same candidate; the participant's action log as a timeline; and the planted diff, marked
reviewer-facing and shown only because the record exists, i.e. after the session ended.

Hard rules: no score, no rank, no hire signal, no aggregate across candidates. Elapsed time appears as a recorded value
in its own section and is never set beside the stranger's (the stranger's elapsed time is not shown at all).
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from backend.grounding.html import CSS, VERDICT_SHAPES, Page, assert_self_contained, assert_traceable

E = html.escape
EXTRA_CSS = """
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.side{border:1px solid var(--line);border-radius:6px;padding:12px}
.side h3{margin-top:0}
.big{font-size:1.6rem;font-weight:700}
ol.timeline{list-style:none;padding-left:0;margin:0}
ol.timeline li{display:grid;grid-template-columns:3.5em 7.5em minmax(0,1fr);gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}
ol.timeline .kind{font-weight:700}
pre.diff{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--code);border-radius:4px;padding:8px;
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
"""


class SessionPage(Page):
    def outcome_badge(self, found: bool | None, ptr: tuple) -> str:
        if found is None:
            return f'<span class="verdict-badge v-unverifiable">{VERDICT_SHAPES["unverifiable"]}not measured</span>'
        key = "supported" if found else "no_evidence"
        word = "found" if found else "not found within budget"
        return f'<span class="verdict-badge v-{key}">{VERDICT_SHAPES[key]}{E(word)}</span>'  # a word and a shape, no number'

    def cost(self, ptr: tuple) -> str:
        v = self.get(*ptr)
        if v is None:
            return self.val(ptr)
        if v == "unbounded":
            return f'{self.val(ptr)} <span class="small muted">(the bug was not localised within the budget)</span>'
        return f'<span class="big">{self.val(ptr)}</span> <span class="small muted">actions from first opening the file to the answer</span>'

    def target(self) -> str:
        """The résumé said X, the bug was planted in X, here is what happened."""
        t = self.d.get("target")
        if t is None:
            return ""
        tp, w = ("target",), ("target", "what_happened")
        happened = (f'{self.val(w + ("status",), "label")}; localisation cost {self.val(w + ("localisation_cost",))}')
        if not t.get("cited"):
            return (f'<section id="target"><h2>Claim targeted</h2><p>{self.val(tp + ("message",))}</p>'
                    f'<p class="small">Planted instead, by explicit choice, in <span class="mono">{self.val(tp + ("planted_module",))}</span> '
                    f'line {self.val(tp + ("planted_line",))}. What happened: {happened}.</p></section>')
        key = t.get("verdict") if t.get("verdict") in VERDICT_SHAPES else "unverifiable"
        inside = "inside" if t.get("inside_cited_lines") else "outside"
        lines = (f'lines {self.val(tp + ("cited_lines", 0))}–{self.val(tp + ("cited_lines", 1))}'
                 if isinstance((t.get("cited_lines") or [None])[0], int) else "no line span")
        return ('<section id="target"><h2>Claim targeted</h2><dl class="kv">'
                f'<dt>the résumé said</dt><dd>“{self.val(tp + ("claim_text",))}” '
                f'<span class="small muted">({self.val(tp + ("claim_kind",), "label")}, claim {self.val(tp + ("claim_id",))})</span></dd>'
                f'<dt>claim verdict</dt><dd><span class="verdict-badge v-{key}">{VERDICT_SHAPES[key]}{self.val(tp + ("verdict",), "label")}</span> '
                f'<span class="small muted">citing <span class="mono">{self.val(tp + ("cited_module",))}</span>, {lines}</span></dd>'
                f'<dt>the bug was planted in</dt><dd><span class="mono">{self.val(tp + ("planted_module",))}</span> line '
                f'{self.val(tp + ("planted_line",))} <span class="small muted">({inside} the cited lines)</span></dd>'
                f'<dt>what happened</dt><dd>{happened}</dd></dl>'
                '<p class="small muted">The claim was never shown during the hunt: the participant and the stranger saw only '
                'the repository and the failing test.</p></section>')

    def render(self) -> str:
        d = self.d
        if d.get("refused"):
            out = (f'<section id="refused"><h2>No session</h2><p>{self.val(("message",))}</p>'
                   f'<p class="small">Reason recorded: {self.val(("reason",))}. Nothing was planted.</p>')
            if d.get("result") == "no_claimed_module":
                ct = ("claim_targeting",)
                out += (f'<p class="small">{self.val(("note",))}. Of {self.val(ct + ("of",))} pooled candidates, '
                        f'{self.val(ct + ("candidates_in_claimed_modules",))} are in a module the résumé claims.</p>')
            return out + "</section>"
        c = ("candidate",)
        out = [f'<section id="candidate"><h2>Candidate</h2><dl class="kv">'
               f'<dt>module</dt><dd class="mono">{self.val(c + ("module",))}</dd>'
               f'<dt>operator</dt><dd class="mono">{self.val(c + ("operator",))} ({self.val(c + ("family",))})</dd>'
               f'<dt>killing test</dt><dd class="mono">{self.val(c + ("killing_test",))}</dd>'
               f'<dt>candidate id</dt><dd class="mono small">{self.val(c + ("id",))}</dd></dl>'
               '<p class="small muted">The participant saw the repository, this failing test and its output, and nothing '
               'else. The participant and the stranger had the same four actions: open a file, search, run tests, answer.</p></section>']

        out.append(self.target())
        h = ("headline",)
        stranger = self.get("comparison", "stranger")
        out.append('<section id="outcome"><h2>Localisation on this candidate</h2><div class="pair">')
        out.append(f'<div class="side"><h3>Participant</h3>{self.outcome_badge(self.get(*h, "found", "participant"), h + ("found", "participant"))}'
                   f'<p>localisation cost: {self.cost(h + ("localisation_cost", "participant"))}</p></div>')
        if stranger is None:
            out.append('<div class="side"><h3>Stranger</h3><p>not measured: no stranger run on this candidate.</p></div>')
        else:
            s = ("comparison", "stranger")
            out.append(f'<div class="side"><h3>Stranger <span class="small muted">({self.val(s + ("tier",))} model, '
                       f'budget {self.val(s + ("budget",))} actions)</span></h3>'
                       f'{self.outcome_badge(self.get(*h, "found", "stranger"), h + ("found", "stranger"))}'
                       f'<p>localisation cost: {self.cost(h + ("localisation_cost", "stranger"))}</p></div>')
        out.append(f'</div><p class="small muted">{self.val(h + ("definition",))}. Same candidate, same four actions. '
                   'This is a fact about one hunt, not a score.</p></section>')

        out.append('<section id="timeline"><h2>Participant actions</h2><ol class="timeline">')
        for i, a in enumerate(d.get("actions") or []):
            b = ("actions", i)
            detail = {"FILE_OPEN": ("path",), "SEARCH": ("pattern",), "TEST_RUN": ("selector",), "ANSWER": ("path",)}.get(a["type"], ())
            parts = [f'<span class="mono">{self.val(b + (k,), missing="(whole suite)" if k == "selector" else "not measured")}</span>' for k in detail]
            if a["type"] == "ANSWER":
                parts.append(f'line {self.val(b + ("line",))}')
            if a["type"] == "SEARCH" and a.get("path"):
                parts.append(f'in <span class="mono">{self.val(b + ("path",))}</span>')
            out.append(f'<li><span class="muted">{self.val(b + ("t",))}s</span><span class="kind">{self.val(b + ("type",), "label")}</span>'
                       f'<span>{" ".join(parts)}</span></li>')
        if not d.get("actions"):
            out.append("<li>no actions were taken</li>")
        out.append('</ol><p class="small muted">Times are seconds since the session started, recorded for the participant '
                   'only.</p></section>')

        out.append('<section id="diff"><h2>The planted change</h2><p class="warn small">Reviewer-facing. Shown only after the '
                   'session ended; the participant never saw it.</p>'
                   f'<pre class="diff">{self.val(("planted_diff",))}</pre></section>')

        o = ("outcome",)
        out.append('<section id="recorded"><h2>Recorded, not compared</h2><dl class="kv small">'
                   f'<dt>outcome</dt><dd>{self.val(o + ("status",), "label")}</dd>'
                   f'<dt>answer</dt><dd class="mono">{self.val(o + ("submitted", "path"))}:{self.val(o + ("submitted", "line"))}</dd>'
                   f'<dt>granularity</dt><dd>{self.val(o + ("granularity",))} (the mutated statement spans lines '
                   f'{self.val(o + ("statement_span", 0))}–{self.val(o + ("statement_span", 1))})</dd>'
                   f'<dt>exact line</dt><dd>{self.val(o + ("correct_exact_line",), "bool")}</dd>'
                   f'<dt>actions used</dt><dd>{self.val(o + ("actions_used",))} of a budget of {self.val(("budget",))}</dd>'
                   f'<dt>distinct files opened</dt><dd>{self.val(o + ("distinct_files_opened",))}</dd>'
                   f'<dt>elapsed (participant)</dt><dd>{self.val(o + ("elapsed_seconds",))} s</dd></dl>'
                   '<p class="small muted">Elapsed time is recorded for the participant and is never set beside the '
                   "stranger's: a model's speed says nothing about understanding.</p></section>")
        return "".join(out)


def render_session(record: dict, title: str = "Bug hunt session") -> str:
    pg = SessionPage(record)
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, '
            f'initial-scale=1"><meta name="color-scheme" content="light dark"><title>{E(title)}</title><style>{CSS}{EXTRA_CSS}</style>'
            f'</head><body><main><header><h1>{E(title)}</h1><p class="lede">One planted bug, one hunt. What the participant did, '
            "and how many actions it took to localise the bug, beside the same measure for a stranger. No score, no rank, no "
            f"hiring signal.</p></header>{pg.render()}</main></body></html>")
    assert_traceable(page, record)
    assert_self_contained(page)
    return page


def write(record_path: Path, out: Path) -> Path:
    out.write_text(render_session(json.loads(Path(record_path).read_text())))
    return out
