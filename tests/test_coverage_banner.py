"""Stage 1 item 4: a partial analysis is loud. "Analysed N of M units (X%) - cost cap reached" at the TOP of the page,
and a report cannot render as complete when units_processed < units_total."""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace as NS

import pytest

from backend.claims import config, pipeline
from backend.grounding.html import TraceError, render_html
from backend.grounding.report import coverage_of
from test_claims_pipeline import fake  # noqa: F401  (the fixture)
from test_report_page import _text, degraded  # noqa: F401  (the fixture)


def ext(total, done):
    return NS(units_total=total, units_processed=done, excluded=[NS(reason_code="cost_cap")] * (total - done))


def test_coverage_counts_units_and_claims():
    c = coverage_of(ext(74, 13), NS(not_judged=["c9"], truncated=True), 9)
    assert (c["units_processed"], c["units_total"], c["units_ratio"], c["extraction_reason"]) == (13, 74, 0.1757, "cost cap reached")
    assert (c["claims_judged"], c["claims_total"], c["verdicts_reason"], c["complete"]) == (9, 10, "verdict cost cap reached", False)
    assert coverage_of(ext(20, 20), NS(not_judged=[], truncated=False), 5)["complete"] is True
    assert coverage_of(None, None, 0) == {}  # not measured is not "complete"


def test_a_partial_analysis_says_so_first(degraded):  # noqa: F811
    d = copy.deepcopy(degraded)
    d["header"]["coverage"] = coverage_of(ext(74, 13), NS(not_judged=[], truncated=False), len(d["rows"]))
    page = render_html(d)
    assert re.findall(r'<section id="([\w-]+)"', page)[0] == "partial-analysis"
    text = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", _text(page)))  # _text pads every value span with spaces
    assert "Analysed 13 of 74 units (17.6%) - cost cap reached" in text


def test_a_report_cannot_render_as_complete_when_units_were_not_processed(degraded):  # noqa: F811
    d = copy.deepcopy(degraded)
    d["header"]["coverage"] = dict(coverage_of(ext(74, 13), None, 0), complete=True)  # forged
    with pytest.raises(TraceError, match="coverage says complete"):
        render_html(d)


def test_a_complete_analysis_has_no_banner(degraded):  # noqa: F811
    d = copy.deepcopy(degraded)
    d["header"]["coverage"] = coverage_of(ext(20, 20), NS(not_judged=[], truncated=False), len(d["rows"]))
    assert 'id="partial-analysis"' not in render_html(d)


def test_the_default_cap_scales_with_document_size():
    assert config.cost_cap_for(3) == config.DEFAULT_COST_CAP_USD  # the old floor for a short document
    assert config.cost_cap_for(74) == round(74 * config.PER_UNIT_NANO_CALLS * config.NANO_REFERENCE_CALL_USD, 6)
    assert config.cost_cap_for(74) > 14 * config.DEFAULT_COST_CAP_USD


def test_the_pipeline_records_units_processed_against_total(fake):  # noqa: F811
    text = "\n".join(f"• Built service number {i} for the payments team" for i in range(6))
    out = pipeline.extract_from_text(text, headings=["EXPERIENCE"], decomposition_tier="nano", cost_cap_usd=0.00025)
    assert out.truncated and out.units_total == 6 and out.units_processed < 6
    full = pipeline.extract_from_text(text, headings=["EXPERIENCE"], decomposition_tier="nano")
    assert (full.units_processed, full.units_total, full.truncated) == (6, 6, False)
