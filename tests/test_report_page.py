"""Phase 4H: the report page. Offline: real fixture repos, scripted judgements, no model calls."""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backend.contracts import ClaimEvidenceReport  # noqa: E402
from backend.grounding.html import TraceError, assert_self_contained, assert_traceable, render_html  # noqa: E402
from backend.grounding.report import NOT_MEASURED, fmt_ratio  # noqa: E402


@pytest.fixture(scope="module")
def degraded(tmp_path_factory):
    import degraded_report_fixture as D
    data = D.build(tmp_path_factory.mktemp("degraded"))
    ClaimEvidenceReport.model_validate(data)
    return data


@pytest.fixture(scope="module")
def page(degraded):
    return render_html(degraded)


def _text(page: str) -> str:
    body = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _rows(page: str) -> list[str]:
    return re.findall(r"<tr>(.*?)</tr>", page.split("<tbody>")[1], flags=re.S)


# --- H0c -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("r,shown", [(0.61538, "61.5%"), (1.0, "100%"), (0.0, "0%"), (0.0004, "0%"), (0.125, "12.5%"),
                                     (None, NOT_MEASURED)])
def test_ratio_is_a_percentage_with_at_most_one_decimal(r, shown):
    assert fmt_ratio(r) == shown


def test_zero_line_languages_are_omitted(degraded):
    for a in degraded["header"]["analysed"]:
        assert a["languages"] and all(n for n in a["languages"].values()) and "Text only" not in a["languages"]


def test_confidence_reason_is_repeated_on_every_cited_row(degraded, page):
    cited = [r for r in degraded["rows"] if r["citation"]]
    assert cited and all(r["authorship_confidence_reason"] for r in cited)
    for row in _rows(page):
        if "file authored" in row:
            assert "confidence reason:" in row and "/authorship_confidence_reason" in row and "/authorship_confidence\"" in row


# --- tracing: format, never calculate --------------------------------------------------------------------------------

def test_every_number_on_the_page_traces_to_the_json(degraded, page):
    assert assert_traceable(page, degraded) > 50


def test_a_tampered_number_is_caught(degraded, page):
    bad = page.replace('data-fmt="text">4</span>', 'data-fmt="text">5</span>', 1)
    assert bad != page
    with pytest.raises(TraceError, match="shows '5'"):
        assert_traceable(bad, degraded)


def test_an_untraced_number_in_static_text_is_caught(degraded, page):
    with pytest.raises(TraceError, match="untraced number"):
        assert_traceable(page.replace("<h2>Shield</h2>", "<h2>Shield (3 findings)</h2>"), degraded)


def test_a_calculated_value_cannot_be_rendered(degraded):
    """A renderer that summed or counted would have to emit a number with no field behind it."""
    import backend.grounding.html as H
    orig = H.Page.not_checkable
    H.Page.not_checkable = lambda self: orig(self).replace("</h2>", f" ({len(self.get('header', 'not_checkable'))})</h2>", 1)
    try:
        with pytest.raises(TraceError):
            render_html(degraded)
    finally:
        H.Page.not_checkable = orig


# --- missing fields --------------------------------------------------------------------------------------------------

def test_missing_fields_render_not_measured_never_blank_or_zero(degraded):
    d = copy.deepcopy(degraded)
    row = next(r for r in d["rows"] if r["citation"])
    for k in ("authored_ratio", "authorship_confidence", "authorship_confidence_reason", "authored_by_candidate"):
        row.pop(k)
    d["header"]["provenance"] = {}
    d["header"]["grounding_failures"] = None
    d["header"].pop("shield")
    d["header"]["authorship"][0]["authored_ratio"] = None
    page = render_html(d)
    assert not re.search(r'data-fmt="\w+"( class="[^"]*")?></span>', page)  # nothing renders blank
    text = _text(page)
    assert text.count(NOT_MEASURED) >= 15
    assert "grounding did not run" in text and "the Shield did not run" in text
    assert re.search(r'/rows/\d+/authored_ratio" data-fmt="ratio">not measured<', page)
    assert re.search(r'/header/provenance/cost_usd" data-fmt="usd">not measured<', page)


# --- page rules ------------------------------------------------------------------------------------------------------

def test_no_score_rank_total_or_hire_signal(page):
    text = _text(page.split("</header>", 1)[1]).lower()  # the lede's disclaimer ("there is no score...") aside
    for word in ("score", "rank ", "ranked", "total", "overall", "recommend hire", "fit for", "pass rate"):
        assert word not in text, word
    top = page.split('id="claims"')[0]
    top = _text(re.sub(r'<pre class="span">.*?</pre>', "", top, flags=re.S)).lower()  # quoted Shield spans are resume text
    for v in ("supported", "contradicted", "no evidence", "inconclusive", "unverifiable"):
        assert v not in top  # nothing summarising verdicts above the claims


def test_every_verdict_has_its_basis_and_reason_adjacent(page):
    rows = _rows(page)
    assert len(rows) == 5
    for row in rows:
        cells = re.findall(r'<td class="(\w+)', row)
        assert cells[:3] == ["claim", "verdict", "reason"]
        verdict_cell = re.search(r'<td class="verdict">(.*?)</td>', row, re.S).group(1)
        assert "verdict-badge" in verdict_cell and "<svg" in verdict_cell
        assert ("samples agreeing" in verdict_cell or "samples disagreed" in verdict_cell
                or "decided by code, no model" in verdict_cell)
        assert re.search(r'<td class="reason"><span data-src="/rows/\d+/reason"', row)


def test_authorship_ratio_never_without_confidence(page):
    for m in re.finditer(r'data-fmt="ratio">', page):
        window = page[m.start(): m.start() + 1500]
        if "/provenance/" in page[m.start() - 80: m.start()]:
            continue  # the greedy-disagreement rate is a rate of samples, not an authorship ratio
        assert "authorship_confidence" in window or "/authorship_confidence\"" in window


def test_verdicts_differ_by_word_and_shape(page):
    from backend.grounding.html import VERDICT_SHAPES
    assert len(set(VERDICT_SHAPES.values())) == len(VERDICT_SHAPES)
    for row in _rows(page):
        m = re.search(r'verdict-badge v-(\w+)">(<svg.*?</svg>)<span[^>]*>([^<]+)<', row, re.S)
        assert m and m.group(2) == VERDICT_SHAPES[m.group(1)] and m.group(3) == m.group(1).replace("_", " ")


def test_self_contained_and_themeable(page):
    assert_self_contained(page)
    assert "http://" not in page and "https://" not in page and "<script" not in page and "<link" not in page
    assert "@media (prefers-color-scheme: dark){:root{" in page and "--v-supported" in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert ".scroll{overflow-x:auto" in page and "overflow-wrap:anywhere" in page
    with pytest.raises(AssertionError):
        assert_self_contained(page.replace("<style>", '<link rel="stylesheet" href="https://cdn.example/x.css"><style>'))


def test_sections_in_order(page):
    ids = re.findall(r'<section id="([\w-]+)"', page)
    assert ids == ["analysed", "shield", "claims", "not-checkable", "excluded", "provenance"]


# --- the degraded report is legible and claims no more than the data -------------------------------------------------

def test_degraded_report_shows_every_degradation(degraded, page):
    h = degraded["header"]
    gap = next(a for a in h["authorship"] if a["repo"].endswith("/identity_gap"))
    none = next(a for a in h["authorship"] if a["repo"].endswith("/single_commit_import"))
    assert gap["likely_identity_gap"] is True and none["authorship_confidence"] == "none"
    assert h["grounding_failures"]["c_path"] == 1 and h["shield"]["status"] == "hidden_text_found"
    assert len(h["not_checkable"]) == 2 and h["provenance"]["ensemble_disagreements"] == 1
    text = _text(page)
    assert "Possible identity gap" in text and "not meaningful for this repository" in text
    assert "hidden or suspicious content" in text and "Degraded fixture, not a live run" in text
    assert "grounding check c_path failed" in text and "samples disagreed: largest agreeing share" in text
    assert "These claims were not searched" in text
    # no row claims support the data cannot carry
    assert all(r["verdict"] != "supported" for r in degraded["rows"])


def test_committed_degraded_fixture_still_validates_and_renders():
    """tests/fixtures/reports/degraded.json is a committed fixture (scripts/degraded_report_fixture.py --out ...):
    a contract change that breaks it, or a renderer change that cannot trace it, fails here."""
    import json
    data = json.loads((ROOT / "tests" / "fixtures" / "reports" / "degraded.json").read_text())
    ClaimEvidenceReport.model_validate(data)
    page = render_html(data)
    assert assert_traceable(page, data) > 50 and page.count('<pre class="span">') == 2
    assert "reason before grounding replaced it" in page and "degraded-fixture" in page


# --- J2: resume identity vs operator identity ----------------------------------------------------------------------

def _doc(*links):
    from types import SimpleNamespace as NS
    return NS(structure=NS(links=[NS(kind="github", github_account=a, github_target=t) for a, t in links]))


@pytest.mark.parametrize("links,user,repos,status,analysed,source", [
    ([("jordan-lee-demo", None)], "hellstar-lab", ["hellstar-lab/Crucible"], "mismatch", ["hellstar-lab/Crucible"], "operator"),
    ([("Hellstar-Lab", None)], "hellstar-lab", ["hellstar-lab/Crucible"], "agree", ["hellstar-lab/Crucible"], "operator"),
    ([], "hellstar-lab", ["hellstar-lab/Crucible"], "no_link", ["hellstar-lab/Crucible"], "operator"),
    ([("sam", "sam/cachekit")], "sam", [], "agree", ["sam/cachekit"], "resume"),
    ([("acme", "acme/platform")], "sam", [], "unconfirmed", ["acme/platform"], "resume"),
])
def test_identity_check(links, user, repos, status, analysed, source):
    from backend.grounding.identity import compare
    ic, use = compare(_doc(*links), user, repos)
    assert (ic["status"], use, ic["repos_source"]) == (status, analysed, source)
    if status == "mismatch":
        assert "jordan-lee-demo" in ic["message"] and "hellstar-lab" in ic["message"]
    if status == "no_link":
        assert ic["message"].startswith("No GitHub link found in the document")
    if source == "resume":
        assert "the repositories the resume names were analysed" in ic["message"]


def test_mismatch_is_at_the_top_and_on_every_authorship_cell(degraded):
    from backend.grounding.identity import compare
    d = copy.deepcopy(degraded)
    d["header"]["identity_check"], _ = compare(_doc(("jordan-lee-demo", None)), "alex-sample-demo", ["x/y"])
    page = render_html(d)
    ids = re.findall(r'<section id="([\w-]+)"', page)
    assert ids[0] == "identity-mismatch" and ids[1:] == ["analysed", "shield", "claims", "not-checkable", "excluded", "provenance"]
    rows = _rows(page)
    assert rows and all("identity mismatch: see the top" in re.search(r'<td class="auth[^"]*">(.*?)</td>', r, re.S).group(1) for r in rows)
    assert "<h2>Identity mismatch</h2>" in page.split('id="analysed"')[0]
    # K4a: the full sentence appears ONCE (the banner); rows and the Analysed section carry a short marker only
    assert _text(page).count("The code below is evidence about") == 1


def test_agreement_is_recorded_without_a_banner(degraded):
    from backend.grounding.identity import compare
    d = copy.deepcopy(degraded)
    d["header"]["identity_check"], _ = compare(_doc(("alex-sample-demo", None)), "alex-sample-demo", ["x/y"])
    page = render_html(d)
    assert 'id="identity-mismatch"' not in page and "identity mismatch" not in page and "agree" in _text(page)



def test_excluded_reasons_report_lines_whenever_they_report_files(degraded):
    """K4b: both or neither. A reason with files always shows its line count, 0 included."""
    for e in degraded["header"]["excluded"]:
        assert set(e["files_by_reason"]) == set(e["lines_by_reason"])
    assert "lines not measured" not in _text(render_html(degraded)).split("Code never used as evidence")[1].split("Could not process")[0]


# --- L5: two kinds of absence --------------------------------------------------------------------------------------

def test_excluded_text_is_split_into_not_a_claim_and_could_not_process(degraded, page):
    units = {u["unit_id"]: u for u in degraded["header"]["excluded_units"]}
    assert units["u4"]["category"] == "could_not_process" and units["u4"]["text"] == "Rewrote it in Go for the platform team."
    assert units["u5"]["category"] == "not_a_claim" and units["u0"]["category"] == "not_a_claim"
    assert units["u0"]["text"] is None  # contact details never reproduced
    sec = _text(page.split('id="excluded"')[1].split("</section>")[0])
    cnp, nac = sec.split("Could not process")[1].split("Not a claim")
    assert "Rewrote it in Go" in cnp and "cannot be disambiguated" in cnp and "Passionate" not in cnp
    assert "Passionate about clean architecture" in nac and "contact details are not reproduced" in nac
    assert "alex@example.com" not in page
