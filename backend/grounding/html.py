"""Phase 4H: the report page. A ClaimEvidenceReport (as JSON) -> one self-contained HTML file.

The renderer FORMATS, it never CALCULATES. Every value on the page is emitted by ``Page.val`` as
``<span data-src="/json/pointer" data-fmt="name">text</span>``; ``assert_traceable`` parses the finished page and checks
that (a) every text node containing a digit sits inside such a span and (b) every span's text is exactly
``FORMATS[fmt](value at that pointer)``, or "not measured" when the field is missing or null. Static labels therefore
carry no digits. ``render_html`` runs the check on every page it returns, so an untraced number cannot ship.

Page rules (binding):
  * no score, rank, total, hire signal or verdict tally anywhere, and nothing summarising at the top;
  * a verdict is never shown without the basis it was reached on (sample agreement and tier, or the code rule that
    decided it without a model) and its reason in the adjacent column;
  * an authored ratio is never shown without its confidence and the confidence reason;
  * a missing field renders "not measured", never blank and never zero;
  * no network: no external CSS, fonts, scripts or images; opens from file://;
  * colours are tokens, redefined under prefers-color-scheme: dark; verdicts differ by word AND shape, not colour;
  * readable at phone width: the page never scrolls sideways, the claims table scrolls in its own container.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from .report import NOT_MEASURED, fmt_ratio

# --------------------------------------------------------------------------------------------------------------------
# tracing
# --------------------------------------------------------------------------------------------------------------------

_MISSING = object()


def pointer(*parts) -> str:
    """RFC 6901 JSON pointer."""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def resolve(data, ptr: str):
    cur = data
    for raw in ptr.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not key.isdigit() or int(key) >= len(cur):
                return _MISSING
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            if key not in cur:
                return _MISSING
            cur = cur[key]
        else:
            return _MISSING
    return cur


def _usd(v) -> str:
    t = f"{v:.6f}".rstrip("0").rstrip(".")
    return f"${t}"


def _text(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "none"
    return str(v)


FORMATS = {
    "text": _text,
    "label": lambda v: str(v).replace("_", " "),  # enum values: no_evidence -> no evidence
    "ratio": fmt_ratio,  # 0..1 -> percentage, at most one decimal
    "usd": _usd,
    "bool": lambda v: "yes" if v else "no",
    "sha": lambda v: str(v)[:12],
    "int": lambda v: f"{v:,}",  # thousands separators only
}


class TraceError(AssertionError):
    """A number on the page does not trace to a field of the report JSON, or a traced span does not match its field."""


class _Checker(HTMLParser):
    def __init__(self, data):
        super().__init__(convert_charrefs=True)
        self.data, self.stack, self.errors, self.spans = data, [], [], 0
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script"):
            self.skip += 1
        if tag in ("br", "hr", "meta", "img", "input", "link", "path", "circle", "rect", "line", "polyline"):
            return  # void / leaf SVG elements: no text inside
        self.stack.append([tag, a, []])

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip -= 1
        if not self.stack or self.stack[-1][0] != tag:
            return
        t, a, buf = self.stack.pop()
        text = "".join(buf)
        if self.stack:
            self.stack[-1][2].append(text)
        if "data-src" in a:
            self.spans += 1
            value = resolve(self.data, a["data-src"])
            if value is _MISSING or value is None:
                if re.search(r"\d", text):
                    self.errors.append(f"{a['data-src']}: missing field rendered with a number: {text!r}")
            else:
                expected = FORMATS[a.get("data-fmt", "text")](value)
                if text != expected:
                    self.errors.append(f"{a['data-src']}: shows {text!r}, the field formats to {expected!r}")

    def handle_data(self, d):
        if self.skip:
            return
        if self.stack:
            self.stack[-1][2].append(d)
        if re.search(r"\d", d) and not any("data-src" in a for _t, a, _b in self.stack):
            self.errors.append(f"untraced number in static text: {d.strip()!r}")


def assert_traceable(page: str, data: dict) -> int:
    """Raise TraceError unless every number on the page traces to the report JSON. Returns the number of traced spans."""
    c = _Checker(data)
    c.feed(page)
    c.close()
    if c.errors:
        raise TraceError("; ".join(c.errors[:10]) + (f" (+{len(c.errors) - 10} more)" if len(c.errors) > 10 else ""))
    return c.spans


def assert_self_contained(page: str) -> None:
    """No network: nothing the browser would fetch."""
    bad = re.findall(r"""(?<![\w-])(?:src|href|srcset|action|poster)\s*=\s*["'](?!#)[^"']*|@import|url\(|<link\b|<script\b|"""
                     r"""<iframe\b|<object\b|<embed\b|<img\b""", page, re.I)
    if bad:
        raise AssertionError(f"the page references external resources: {bad[:5]}")


# --------------------------------------------------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------------------------------------------------

E = html.escape

VERDICT_SHAPES = {  # word + shape; colour is a third, redundant cue
    "supported": '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="2" fill="currentColor"/></svg>',
    "contradicted": '<svg viewBox="0 0 16 16" aria-hidden="true"><line x1="3" y1="3" x2="13" y2="13" stroke="currentColor" stroke-width="3"/>'
                    '<line x1="13" y1="3" x2="3" y2="13" stroke="currentColor" stroke-width="3"/></svg>',
    "no_evidence": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
    "inconclusive": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="2"/>'
                    '<path d="M8 2.5 A5.5 5.5 0 0 1 8 13.5 Z" fill="currentColor"/></svg>',
    "unverifiable": '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" fill="none" stroke="currentColor" '
                    'stroke-width="2" stroke-dasharray="3 2"/></svg>',
}
UNKNOWN_SHAPE = '<svg viewBox="0 0 16 16" aria-hidden="true"><line x1="3" y1="8" x2="13" y2="8" stroke="currentColor" stroke-width="2"/></svg>'

CSS = """
:root{--bg:#f7f6f2;--panel:#ffffff;--fg:#1c1b19;--muted:#5f5c55;--line:#d9d5cb;--code:#efece5;--warn-bg:#fdf3dc;--warn-fg:#6b4a00;
--v-supported:#1d6b40;--v-contradicted:#a3261b;--v-no_evidence:#4a5160;--v-inconclusive:#7c5300;--v-unverifiable:#55534e;color-scheme:light dark}
@media (prefers-color-scheme: dark){:root{--bg:#151513;--panel:#1e1e1b;--fg:#ebe9e3;--muted:#a9a59b;--line:#3a3933;--code:#2a2925;
--warn-bg:#3a2f12;--warn-fg:#f3d58a;--v-supported:#6fcf97;--v-contradicted:#ff8a7a;--v-no_evidence:#aab3c5;--v-inconclusive:#f0bf5a;
--v-unverifiable:#b9b6ad}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
overflow-wrap:anywhere}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.45rem;margin:0 0 4px}
h2{font-size:1.05rem;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em}
h3{font-size:.95rem;margin:16px 0 6px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin:16px 0}
.lede{color:var(--muted);margin:0}
.muted{color:var(--muted)}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
code{background:var(--code);padding:1px 4px;border-radius:4px}
dl.kv{display:grid;grid-template-columns:minmax(0,max-content) minmax(0,1fr);gap:4px 16px;margin:0}
dl.kv dt{color:var(--muted)}
dl.kv dd{margin:0}
@media (max-width:560px){dl.kv{grid-template-columns:minmax(0,1fr)}dl.kv dd{margin-bottom:6px}}
ul{margin:4px 0;padding-left:20px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;min-width:1040px}
th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--line)}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--code)}
tr:last-child td{border-bottom:0}
td.claim{width:20%}td.verdict{width:15%}td.reason{width:19%}td.cite{width:15%}td.auth{width:19%}td.ground{width:12%}
.verdict-badge{display:inline-flex;align-items:center;gap:6px;font-weight:700;white-space:nowrap}
.verdict-badge svg{width:14px;height:14px;flex:none}
.v-supported{color:var(--v-supported)}.v-contradicted{color:var(--v-contradicted)}.v-no_evidence{color:var(--v-no_evidence)}
.v-inconclusive{color:var(--v-inconclusive)}.v-unverifiable{color:var(--v-unverifiable)}
.basis{font-size:.85rem;margin-top:6px}
.small{font-size:.85rem}
.warn{background:var(--warn-bg);color:var(--warn-fg);border-radius:4px;padding:2px 6px;display:inline-block;margin-top:4px}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin:0 0 12px;font-size:.85rem}
blockquote{margin:0;padding:0 0 0 10px;border-left:3px solid var(--line)}
pre.span{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--code);border-radius:4px;padding:6px 8px;margin:4px 0 10px;
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
ol.shield-items{padding-left:22px}
ol.questions{padding-left:22px}ol.questions li{margin-bottom:14px}p.question{margin:0 0 4px;font-size:1.02rem}
section.alert{border:2px solid var(--v-contradicted);background:var(--warn-bg)}
section.alert h2{color:var(--v-contradicted)}
"""


class Page:
    def __init__(self, data: dict):
        self.d = data

    def val(self, parts, fmt: str = "text", missing: str = NOT_MEASURED, cls: str | None = None) -> str:
        """The ONLY way a value reaches the page: formatted, escaped, and tagged with its JSON pointer."""
        ptr = pointer(*parts)
        v = resolve(self.d, ptr)
        text = missing if (v is _MISSING or v is None) else FORMATS[fmt](v)
        c = f' class="{cls}"' if cls else ""
        return f'<span data-src="{E(ptr)}" data-fmt="{fmt}"{c}>{E(text)}</span>'

    def get(self, *parts):
        v = resolve(self.d, pointer(*parts))
        return None if v is _MISSING else v

    # ---- sections ------------------------------------------------------------------------------------------------
    def _mismatch(self) -> bool:
        return self.get("header", "identity_check", "status") == "mismatch"

    def identity_banner(self) -> str:
        """J2: a mismatch between the resume's GitHub identity and the analysed account, at the TOP of the page."""
        if not self._mismatch():
            return ""
        b = ("header", "identity_check")
        return (f'<section id="identity-mismatch" class="alert" role="alert"><h2>Identity mismatch</h2>'
                f'<p>{self.val(b + ("message",))}</p><dl class="kv small">'
                f'<dt>GitHub named in the resume</dt><dd class="mono">{self.val(b + ("resume_accounts",))}</dd>'
                f'<dt>account analysed (operator)</dt><dd class="mono">{self.val(b + ("operator_user",))}</dd></dl></section>')

    def analysed(self) -> str:
        out = ['<section id="analysed"><h2>Analysed</h2>']
        cand = self.get("header", "candidate")
        out.append('<dl class="kv"><dt>candidate (as supplied by the operator)</dt><dd>')
        if cand is None:
            out.append(f"{NOT_MEASURED}: no candidate identifier was supplied, so this report cannot say whose it is")
        else:
            parts = []
            if "github_user" in cand:
                parts.append(f'GitHub user <strong class="mono">{self.val(("header", "candidate", "github_user"))}</strong>')
            if "reference" in cand:
                parts.append(f'reference <strong class="mono">{self.val(("header", "candidate", "reference"))}</strong>')
            out.append(" · ".join(parts) or NOT_MEASURED)
        out.append("</dd>")
        ic = ("header", "identity_check")
        if self.get(*ic) is None:
            out.append(f"<dt>resume GitHub link</dt><dd>{NOT_MEASURED}: the resume's links were not compared with the analysed account</dd>")
        elif self._mismatch():
            out.append(f"<dt>resume GitHub link</dt><dd><strong>{self.val(ic + ('status',), 'label')}</strong>: "
                       "see the banner at the top of the report</dd>")
        else:
            out.append(f"<dt>resume GitHub link</dt><dd><strong>{self.val(ic + ('status',), 'label')}</strong>: "
                       f"{self.val(ic + ('message',))}</dd>")
        out.append("</dl>")
        repos = self.get("header", "analysed") or []
        if not repos:
            out.append(f"<p>{NOT_MEASURED}: no repository was analysed in this report.</p>")
        auth = self.get("header", "authorship") or []
        for i, a in enumerate(repos):
            base = ("header", "analysed", i)
            name = base + ("repo",) if "repo" in a else base + ("input",)
            out.append(f'<h3 class="mono">{self.val(name)}</h3><dl class="kv">')
            out.append(f"<dt>status</dt><dd>{self.val(base + ('status',), 'label')}</dd>")
            if "repo" in a:
                out.append(f"<dt>ref analysed</dt><dd><code>{self.val(base + ('ref',))}</code> at "
                           f"<code>{self.val(base + ('head_sha',), 'sha')}</code></dd>")
                out.append(f"<dt>scope</dt><dd>{self.val(base + ('scope',), missing='whole repository')}</dd>")
                out.append(f"<dt>source files</dt><dd>{self.val(base + ('source_files',))}</dd>")
                out.append(f"<dt>source lines</dt><dd>{self.val(base + ('total_source_lines',))}</dd>")
                langs = a.get("languages")
                if langs:
                    items = ", ".join(f"{E(k)} {self.val(base + ('languages', k))}" for k in langs)
                    out.append(f"<dt>languages (source lines)</dt><dd>{items}</dd>")
                elif langs is None:
                    out.append(f"<dt>languages</dt><dd>{self.val(base + ('languages',))}</dd>")
                else:
                    out.append("<dt>languages</dt><dd>none with source lines</dd>")
            out.append("</dl>")
            j = next((k for k, x in enumerate(auth) if x.get("repo") == a.get("repo")), None)
            if j is not None:
                out.append(self._repo_authorship(("header", "authorship", j)))
            elif "repo" in a:
                out.append(f'<p class="small">Authorship: {NOT_MEASURED}.</p>')
        out.append("</section>")
        return "".join(out)

    def _repo_authorship(self, base) -> str:
        gap = self.get(*base, "likely_identity_gap")
        meaningful = self.get(*base, "meaningful")
        out = ['<dl class="kv small">',
               f"<dt>authored by the candidate</dt><dd>{self.val(base + ('authored_ratio',), 'ratio')} of source lines "
               f"(confidence {self.val(base + ('authorship_confidence',), cls='mono')})</dd>",
               f"<dt>confidence reason</dt><dd>{self.val(base + ('reason',))}</dd>",
               f"<dt>ratio meaningful</dt><dd>{self.val(base + ('meaningful',), 'bool')}</dd>",
               f"<dt>likely identity gap</dt><dd>{self.val(base + ('likely_identity_gap',), 'bool')}</dd></dl>"]
        if gap:
            out.append('<p class="warn small">Possible identity gap: one unmatched author wrote most lines. The candidate may have '
                       'committed under an email not linked to their account. Do not read the authored share as their share.</p>')
        if meaningful is False:
            out.append('<p class="warn small">The authored share is not meaningful for this repository (see the confidence reason).</p>')
        return "".join(out)

    def shield(self) -> str:
        out = ['<section id="shield"><h2>Shield</h2>']
        if self.get("header", "shield") is None:
            out.append(f"<p>{NOT_MEASURED}: no resume document was analysed in this report, so the Shield did not run.</p></section>")
            return "".join(out)
        b = ("header", "shield")
        out.append('<dl class="kv">')
        out.append(f"<dt>document</dt><dd class=\"mono\">{self.val(b + ('document',))}</dd>")
        out.append(f"<dt>status</dt><dd><strong>{self.val(b + ('status',), 'label')}</strong></dd>")
        out.append(f"<dt>extraction used cleaned text only</dt><dd>{self.val(b + ('gate_applied',), 'bool')}</dd>")
        out.append(f"<dt>detectors that fired</dt><dd>{self.val(b + ('detectors',))}</dd>")
        out.append(f"<dt>hidden text fragments</dt><dd>{self.val(b + ('hidden_fragments',))}</dd>")
        out.append(f"<dt>resume units removed</dt><dd>{self.val(b + ('units_removed',))}</dd>")
        out.append(f"<dt>needs human review</dt><dd>{self.val(b + ('needs_review',), 'bool')}</dd></dl>")
        items = self.get(*b, "items")
        if items is None:
            out.append(f'<p class="small">Finding locations and text: {NOT_MEASURED}.</p>')
        elif items:
            out.append('<h3>Findings, with the hidden text as a machine reads it</h3><ol class="shield-items">')
            for i, it in enumerate(items):
                ib = b + ("items", i)
                reasons = f" ({self.val(ib + ('reasons',))})" if it.get("reasons") else ""
                sev = f" · severity {self.val(ib + ('severity',))}" if it.get("severity") else ""
                cut = ' <span class="muted">(shown up to the cap; the original is longer)</span>' if it.get("span_truncated") else ""
                out.append(f'<li><strong>{self.val(ib + ("kind",), "label")}</strong> · {self.val(ib + ("detector",), "label")}'
                           f'{reasons}{sev} · {self.val(ib + ("location",))}'
                           f'<pre class="span">{self.val(ib + ("span",))}</pre>{cut}</li>')
            out.append("</ol>")
        if self.get(*b, "status") not in (None, "clean"):
            out.append('<p class="warn small">The document carried hidden or suspicious content. Claims were extracted from the '
                       'cleaned text only; read the original with this in mind.</p>')
        out.append("</section>")
        return "".join(out)

    def claims(self) -> str:
        rows = self.get("rows") or []
        out = ['<section id="claims"><h2>Claims</h2>',
               '<p class="small muted">One row per claim, in resume order. Each verdict is shown with how it was reached and why. '
               'Verdicts are facts about the evidence, not a judgement of the person.</p>',
               '<div class="legend" aria-label="verdict key">']
        for v in VERDICT_SHAPES:
            out.append(f'<span class="verdict-badge v-{v}">{VERDICT_SHAPES[v]}{E(v.replace("_", " "))}</span>')
        out.append("</div>")
        refs = self.get("header", "references") or []
        if refs:
            out.append('<p class="small"><strong>References</strong> <span class="muted">(project and entry headings: not '
                       'claims, not verified, shown as the resume states them)</span></p><ul class="small">')
            for i, r in enumerate(refs):
                b = ("header", "references", i)
                names = (f'names the analysed repository <span class="mono">{self.val(b + ("names_analysed_repos",))}</span>'
                         if r.get("names_analysed_repos") else "no analysed repository has this name")
                out.append(f'<li>{self.val(b + ("text",))} <span class="muted">· {names}</span></li>')
            out.append("</ul>")
        if not rows:
            out.append(f"<p>{NOT_MEASURED}: the report has no claim rows.</p></section>")
            return "".join(out)
        out.append('<div class="scroll" tabindex="0" role="region" aria-label="claims table"><table><thead><tr>'
                   "<th>Claim</th><th>Verdict and basis</th><th>Reason</th><th>Citation</th><th>Authorship of cited code</th>"
                   "<th>Grounding</th></tr></thead><tbody>")
        for i, r in enumerate(rows):
            out.append(self._row(i, r))
        out.append("</tbody></table></div></section>")
        return "".join(out)

    def _row(self, i: int, r: dict) -> str:
        b = ("rows", i)
        v = r.get("verdict")
        shape = VERDICT_SHAPES.get(v, UNKNOWN_SHAPE)
        claim = (f'<td class="claim"><blockquote>{self.val(b + ("claim_text",))}</blockquote>'
                 f'<div class="small muted">kind: {self.val(b + ("claim_kind",), "label")} · id {self.val(b + ("claim_id",), cls="mono")}</div>')
        if r.get("scope") not in (None, "none"):
            claim += (f'<div class="small muted">scope: {self.val(b + ("scope",), "label")}'
                      + (f' ({self.val(b + ("scope_target",))})' if r.get("scope_target") else "") + "</div>")
        claim += "</td>"

        verdict = [f'<td class="verdict"><span class="verdict-badge v-{E(v or "")}">{shape}{self.val(b + ("verdict",), "label")}</span>',
                   '<div class="basis">']
        if r.get("matrix_row") == "contradiction:not_confirmed":
            verdict.append(f'a contradiction was proposed but not confirmed by both tiers; final tier '
                           f'{self.val(b + ("model_tier",))} agreement {self.val(b + ("agreement",))}')
            if r.get("escalated"):
                verdict.append(f'<br>escalated: {self.val(b + ("escalation_reason",))}')
        elif r.get("agreement") and r.get("matrix_row") == "ensemble:disagreed":
            verdict.append(f'samples disagreed: largest agreeing share {self.val(b + ("agreement",))} '
                           f'({self.val(b + ("model_tier",))} tier), not enough for a verdict')
        elif r.get("agreement"):
            on = " on the verdict before grounding" if (r.get("pre_grounding_verdict") and r.get("pre_grounding_verdict") != v) else ""
            verdict.append(f'samples agreeing{on}: {self.val(b + ("agreement",))} '
                           f'({self.val(b + ("model_tier",))} tier)')
            if r.get("escalated"):
                verdict.append(f'<br>escalated: {self.val(b + ("escalation_reason",))}')
        else:
            verdict.append(f'decided by code, no model: <code>{self.val(b + ("matrix_row",))}</code>')
        pre = r.get("pre_grounding_verdict")
        if pre and pre != v:
            verdict.append(f'<br>before grounding: {self.val(b + ("pre_grounding_verdict",), "label")}')
        verdict.append("</div></td>")

        reason = f'<td class="reason">{self.val(b + ("reason",))}'
        if r.get("content_reason"):
            reason += f'<div class="small muted" style="margin-top:6px">model\'s reading of the evidence: {self.val(b + ("content_reason",))}</div>'
        if r.get("superseded_reason"):
            reason += (f'<div class="small" style="margin-top:6px">reason before grounding replaced it: '
                       f'{self.val(b + ("superseded_reason",))}</div>')
        if r.get("authorship_note"):
            reason += f'<div class="small" style="margin-top:6px">authorship note: {self.val(b + ("authorship_note",))}</div>'
        reason += "</td>"

        mm = ""
        if self._mismatch():
            ic = ("header", "identity_check")
            mm = '<div class="warn">⚠ identity mismatch: see the top of the report</div>'
        if r.get("citation") is None:
            cite = '<td class="cite small muted">no cited item</td>'
            auth = (f'<td class="auth small">{NOT_MEASURED}: no item is cited, so authorship of cited code does not apply.{mm}</td>')
        else:
            c = b + ("citation",)
            lines = (f':{self.val(c + ("start_line",))}–{self.val(c + ("end_line",))}' if r["citation"].get("start_line") is not None
                     else f' <span class="muted">(lines {NOT_MEASURED})</span>')
            cite = (f'<td class="cite"><div class="mono small">{self.val(c + ("repo",))}</div>'
                    f'<div class="mono">{self.val(c + ("path",), missing="(repository-level item)")}{lines}</div></td>')
            auth = [f'<td class="auth small">{mm}',
                    f'<div>candidate wrote the cited lines: <strong>{self.val(b + ("authored_by_candidate",), "bool")}</strong>'
                    + (' <span class="muted">(cited item)</span>' if r.get("claim_kind") == "technology_use" else "") + '</div>',
                    f'<div>file authored: <strong>{self.val(b + ("authored_ratio",), "ratio")}</strong>, confidence '
                    f'<strong>{self.val(b + ("authorship_confidence",), cls="mono")}</strong></div>',
                    f'<div style="margin-top:4px">confidence reason: {self.val(b + ("authorship_confidence_reason",))}</div>']
            auth.append(self._volume(b, r))
            if r.get("likely_identity_gap"):
                auth.append('<div class="warn">possible identity gap in this repository</div>')
            if r.get("authorship_caveat"):
                auth.append(f'<div class="muted" style="margin-top:4px">caveat: {self.val(b + ("authorship_caveat",))}</div>')
            auth.append("</td>")
            auth = "".join(auth)

        checks = r.get("grounding_checks")
        if r.get("citation") is None and checks is None:
            ground = '<td class="ground small muted">not applicable: nothing cited to check</td>'
        elif checks is None:
            ground = f'<td class="ground small">{NOT_MEASURED}</td>'
        else:
            items = "".join(f'<li><code>{E(k)}</code> {self.val(b + ("grounding_checks", k), "label")}</li>' for k in checks)
            ground = f'<td class="ground small">passed: <strong>{self.val(b + ("grounding_passed",), "bool")}</strong><ul>{items}</ul></td>'
        return f"<tr>{claim}{''.join(verdict)}{reason}{cite}{auth}{ground}</tr>"

    def _volume(self, b, r) -> str:
        """K1: how much of the technology is in the candidate's code. Formatting only; the counts come from 4B.
        L1: for technology_use the verdict's authorship is the WHOLE evidence set's, shown first; the cited item's
        authorship above it is the reasoning anchor only."""
        if r.get("claim_kind") != "technology_use":
            return ""
        out = ""
        sa = b + ("set_authorship",)
        if r.get("set_authorship") is None:
            out += f'<div style="margin-top:4px">across the evidence: {NOT_MEASURED}</div>'
        else:
            out += (f'<div style="margin-top:4px"><strong>across all {self.val(sa + ("items",), "int")} evidence items</strong> '
                    f'(this decides the verdict): {self.val(sa + ("authored_items",), "int")} in code the candidate authored, '
                    f'{self.val(sa + ("not_authored_items",), "int")} not, {self.val(sa + ("unknown_items",), "int")} unknown</div>')
        return out + self._volume_line(b, r)

    def _volume_line(self, b, r) -> str:
        vol = r.get("volume")
        if vol is None:
            return f'<div style="margin-top:4px">volume: {NOT_MEASURED}</div>'
        v = b + ("volume",)
        if vol.get("basis") == "authored_lines_in_language":
            return (f'<div style="margin-top:4px">volume: <strong>{self.val(v + ("authored_lines",), "int")}</strong> authored '
                    f'{self.val(v + ("language",))} source lines, of {self.val(v + ("source_lines",), "int")} in the repository</div>')
        return (f'<div style="margin-top:4px">volume: the candidate wrote the import in <strong>'
                f'{self.val(v + ("authored_files",), "int")}</strong> of {self.val(v + ("files",), "int")} files importing '
                f'{self.val(v + ("technology",))}</div>')

    def questions(self) -> str:
        """Phase 4G. The question is what the candidate hears; the claim and verdict beside it are for the reviewer."""
        if self.get("questions") is None:
            return ""
        b = ("questions",)
        qs = self.get("questions", "questions") or []
        person = self.get("questions", "person")
        out = ['<section id="questions"><h2>Interview questions</h2>',
               '<p class="small muted">Chosen and ordered by code (contradicted claims first, then the most complex code the '
               'person wrote, then claims with no evidence, then inconclusive ones); phrased by a model that never saw the '
               'verdicts; every question checked against four rules (only authored code, no accusation, no verdict, no fact '
               'outside its anchor). The verdict beside each question is for the reviewer, not for the candidate.</p>']
        if person:
            out.append(f'<p class="warn small">Per-student sheet: questions anchored only in code written by '
                       f'<span class="mono">{self.val(b + ("person",))}</span>.</p>')
        if not qs:
            out.append("<p>No question passed selection and the four rules.</p>")
        out.append('<ol class="questions">')
        for i, q in enumerate(qs):
            qb = b + ("questions", i)
            a = q["anchor"]
            if a["kind"] == "function":
                anchor = (f'<span class="mono">{self.val(qb + ("anchor", "function"))}</span> · '
                          f'<span class="mono">{self.val(qb + ("anchor", "path"))}:{self.val(qb + ("anchor", "start_line"))}–'
                          f'{self.val(qb + ("anchor", "end_line"))}</span> · complexity {self.val(qb + ("anchor", "ccn"))} · '
                          f'{self.val(qb + ("anchor", "authored_line_ratio"), "ratio")} written by '
                          + ("this student" if person else "the candidate"))
            else:
                anchor = "about the claim only (no code is cited)"
            out.append(f'<li><p class="question"><strong>{self.val(qb + ("text",))}</strong></p>'
                       f'<div class="small">anchor: {anchor}</div>'
                       f'<div class="small muted">for the reviewer: claim {self.val(qb + ("claim_text",))} · verdict '
                       f'{self.val(qb + ("verdict",), "label")}</div></li>')
        out.append("</ol>")
        rej = self.get("questions", "rejected") or []
        skipped = self.get("questions", "skipped") or []
        out.append(f'<p class="small muted">Rejected by the rules: {self.val(b + ("rejected_count",))} · not asked: '
                   f'{self.val(b + ("skipped_count",))} · over the cap of {self.val(b + ("max_questions",))}: '
                   f'{self.val(b + ("dropped_by_cap",))}</p>')
        if rej:
            out.append('<ul class="small">' + "".join(
                f'<li><strong>{self.val(b + ("rejected", j, "rule"))}</strong> {self.val(b + ("rejected", j, "text"))} '
                f'<span class="muted">({self.val(b + ("rejected", j, "detail"))})</span></li>' for j in range(len(rej))) + "</ul>")
        if skipped:
            out.append('<ul class="small muted">' + "".join(
                f'<li>{self.val(b + ("skipped", j, "claim_id"), cls="mono")}: {self.val(b + ("skipped", j, "reason"))}</li>'
                for j in range(len(skipped))) + "</ul>")
        out.append("</section>")
        return "".join(out)

    def not_checkable(self) -> str:
        nc = self.get("header", "not_checkable")
        out = ['<section id="not-checkable"><h2>Not checkable from code</h2>']
        if nc is None:
            out.append(f"<p>{NOT_MEASURED}</p>")
        elif not nc:
            out.append("<p>None: every claim was searched.</p>")
        else:
            out.append('<p class="small muted">These claims were not searched: code cannot answer them. They are neither supported '
                       "nor contradicted.</p><ul>")
            for i, _n in enumerate(nc):
                b = ("header", "not_checkable", i)
                out.append(f'<li><blockquote>{self.val(b + ("claim_text",))}</blockquote><span class="small muted">kind '
                           f'{self.val(b + ("kind",), "label")} · id {self.val(b + ("claim_id",), cls="mono")}</span>'
                           f'<div class="small">{self.val(b + ("skip_reason",))}</div></li>')
            out.append("</ul>")
        out.append("</section>")
        return "".join(out)

    def excluded(self) -> str:
        out = ['<section id="excluded"><h2>Excluded</h2><h3>Code never used as evidence</h3>']
        ex = self.get("header", "excluded")
        if not ex:
            out.append(f"<p>{NOT_MEASURED}</p>")
        for i, e in enumerate(ex or []):
            b = ("header", "excluded", i)
            out.append(f'<p class="mono small">{self.val(b + ("repo",))}</p>')
            if not e.get("files_by_reason"):
                out.append('<p class="small">No files were excluded.</p>')
            else:
                out.append('<dl class="kv small">')
                for k in e["files_by_reason"]:
                    lines = (f', {self.val(b + ("lines_by_reason", k))} lines' if k in (e.get("lines_by_reason") or {})
                             else f", lines {NOT_MEASURED}")
                    out.append(f"<dt>{E(k.replace('_', ' '))}</dt><dd>{self.val(b + ('files_by_reason', k))} files{lines}</dd>")
                out.append("</dl>")
            if e.get("authorship_only"):
                out.append('<p class="small">Still counted as the candidate\'s authored work (blamed, usable as evidence), '
                           'though excluded from line counts and language statistics: fixture code Linguist sets aside '
                           'for statistics only.</p><ul class="small mono">'
                           + "".join(f"<li>{self.val(b + ('authorship_only', j))}</li>" for j in range(len(e["authorship_only"])))
                           + "</ul>")
            if e.get("top_excluded"):
                out.append('<ul class="small mono">' + "".join(f"<li>{self.val(b + ('top_excluded', j))}</li>"
                                                              for j in range(len(e["top_excluded"]))) + "</ul>")
        units = self.get("header", "excluded_units")
        if self.get("header", "shield") is None and not units:
            out.append(f"<h3>Resume text left out of claim extraction</h3><p>{NOT_MEASURED}: no resume document was "
                       "analysed in this report.</p>")
        else:
            # L5: "the candidate did not claim this" and "Crucible could not process this" are different absences
            for cat, title, blurb, empty in (
                    ("could_not_process", "Could not process",
                     "Factual claims the pipeline failed on. The candidate DID claim these; Crucible could not turn them into "
                     "checkable claims, and they are not verdicts of any kind.", "None: every factual claim was processed."),
                    ("not_a_claim", "Not a claim",
                     "Text with no factual assertion (opinion, contact details, formatting text, a bare date).", "None.")):
                rows = [(i, u) for i, u in enumerate(units or []) if u.get("category") == cat]
                out.append(f"<h3>{title}</h3><p class=\"small muted\">{blurb}</p>")
                if not rows:
                    out.append(f"<p class=\"small\">{empty}</p>")
                    continue
                out.append("<ul>")
                for i, u in rows:
                    b = ("header", "excluded_units", i)
                    gone = "(contact details are not reproduced)" if u.get("reason_code") == "contact_info" else NOT_MEASURED
                    text = f'<blockquote>{self.val(b + ("text",), missing=gone)}</blockquote>'
                    out.append(f'<li>{text}<span class="small"><strong>{self.val(b + ("reason_code",), "label")}</strong> at '
                               f'{self.val(b + ("stage",), "label")}: {self.val(b + ("detail",))}</span></li>')
                out.append("</ul>")
        out.append("</section>")
        return "".join(out)

    def provenance(self) -> str:
        p = ("header", "provenance")
        out = ['<section id="provenance"><h2>Provenance</h2>']
        if self.get(*p, "run_label"):
            out.append(f'<p class="warn">{self.val(p + ("run_label",))}</p>')
        out.append("<h3>Grounding failures (citations re-checked against the repository)</h3>")
        gf = self.get("header", "grounding_failures")
        if gf is None:
            out.append(f"<p>{NOT_MEASURED}: grounding did not run.</p>")
        else:
            out.append('<dl class="kv small">' + "".join(f"<dt><code>{E(k)}</code></dt><dd>{self.val(('header', 'grounding_failures', k))}</dd>"
                                                        for k in gf) + "</dl>")
        out.append('<h3>Model use</h3><dl class="kv small">')
        for key, label, fmt in (("model_requests", "verdict requests to the model", "text"),
                                ("samples", "sampled judgements", "text"),
                                ("escalations", "escalations to the Ultra tier", "text"),
                                ("escalations_on_disagreement", "escalated because Super samples disagreed", "text"),
                                ("disagreements_resolved_by_ultra", "of those, Ultra samples unanimous", "text"),
                                ("escalations_on_check", "escalated after a unanimous Super verdict (contradicted or low probability)", "text"),
                                ("ensemble_disagreements", "left inconclusive because samples still disagreed", "text"),
                                ("contradictions_confirmed", "contradictions confirmed by both tiers unanimously", "text"),
                                ("contradictions_not_confirmed", "contradictions one tier did not confirm (left inconclusive)", "text"),
                                ("citation_downgraded", "samples downgraded for an unresolvable citation", "text"),
                                ("greedy_disagreement_rate", "samples whose most probable label differed from the label generated", "ratio"),
                                ("cost_usd", "verdict cost", "usd"),
                                ("cost_cap_usd", "verdict cost cap", "usd"),
                                ("truncated", "cost cap reached", "bool"),
                                ("not_judged", "claims left unjudged by the cap", "text"),
                                ("extraction_requests", "claim-extraction requests", "text"),
                                ("extraction_cost_usd", "claim-extraction cost", "usd")):
            out.append(f"<dt>{E(label)}</dt><dd>{self.val(p + (key,), fmt)}</dd>")
        out.append("</dl><h3>Stage versions</h3>")
        sv = self.get("header", "stage_versions") or {}
        out.append('<dl class="kv small">' + ("".join(f"<dt>{E(k)}</dt><dd><code>{self.val(('header', 'stage_versions', k))}</code></dd>"
                                                     for k in sv) or f"<dt>versions</dt><dd>{NOT_MEASURED}</dd>") + "</dl>")
        out.append(f'<h3>Reproducibility</h3><p class="small">{self.val(p + ("reproducibility_note",))}</p>')
        rows = self.get("rows") or []
        hashes = [(i, r) for i, r in enumerate(rows) if r.get("prompt_hash")]
        if hashes:
            out.append('<ul class="small mono">' + "".join(f"<li>{self.val(('rows', i, 'claim_id'))} "
                                                           f"{self.val(('rows', i, 'prompt_hash'))}</li>" for i, _r in hashes) + "</ul>")
        out.append("</section>")
        return "".join(out)


def render_html(data: dict, title: str = "Claim evidence report") -> str:
    """data: a ClaimEvidenceReport as a JSON-compatible dict. Returns the page; raises if anything is untraced."""
    if re.search(r"\d", title):
        raise TraceError("the page title is static text and may not carry numbers")
    pg = Page(data)
    body = "".join([pg.identity_banner(), pg.analysed(), pg.shield(), pg.claims(), pg.questions(), pg.not_checkable(),
                    pg.excluded(), pg.provenance()])
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{E(title)}</title><style>{CSS}</style></head><body><main>'
            f'<header><h1>{E(title)}</h1><p class="lede">Facts for a human reviewer: what was analysed, what each claim was checked '
            "against, and how. There is no score, ranking or hiring recommendation, and none should be read into this page.</p>"
            f"</header>{body}</main></body></html>")
    assert_traceable(page, data)
    assert_self_contained(page)
    return page
