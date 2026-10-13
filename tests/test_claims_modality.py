"""Stage 1 items 1-2: modality inheritance and the tense guard. Offline: the scripted FakeLLM from the pipeline tests."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from backend.claims import modality, pipeline, tense
from backend.claims.split import Unit
from test_claims_pipeline import J, fake  # noqa: F401  (the fixture)

# ---- item 2: the tense guard; the three fixtures are from the live OSDBMS phase-II run ---------------------------------
LIVE = [
    ("Task Pending: Develop the dynamic decision script that alters system throughput limits automatically using live "
     "performance metrics; Team Member (to complete the task): Dubey, Pragati.",
     "Developed the dynamic decision script that alters system throughput limits automatically using live performance metrics",
     "tense_changed"),
    ("5. An automated concurrency adjuster scaling throughput in response to live load variations.",
     "An automated concurrency adjuster scales throughput.", "tense_changed"),
    ("We are keeping strictly to our staged schedule: establishing a deterministic baseline first before experimenting with "
     "optional AI extensions.", "We are not experimenting with optional AI extensions.", "negation_inverted"),
]


@pytest.mark.parametrize("source,claim,code", LIVE[2:])
def test_the_live_rewrites_are_rejected(source, claim, code):
    assert tense.check(source, claim)[0] == code


def test_the_future_list_participle_is_left_to_modality():
    """"An automated concurrency adjuster scaling throughput" -> "scales": a PRESENT participle is not a guard construction
    (it produced 7 false positives over five runs). Its unit sits under "Upon completion ... will deliver", a future_lead_in
    section, so modality makes it a non-assertion before any model call."""
    source, claim, _code = LIVE[1]
    assert tense.check(source, claim) is None


def test_the_pending_imperative_is_left_to_modality():
    """Stage 3 item 3: "Develop ..." (a bare imperative) is not one of the guard's constructions. A Pending heading makes
    the unit a non-assertion before any model call (test_a_pending_or_planned_heading_...), so it never reaches Stage 4."""
    source, claim, _code = LIVE[0]
    assert tense.check(source, claim) is None


@pytest.mark.parametrize("source,claim", [
    ("• Built a Redis-backed caching layer serving 12k req/s", "Built a Redis-backed caching layer serving 12k req/s"),
    ("Reduced p95 API latency by 38% by redesigning the PostgreSQL access layer.", "Redesigned the PostgreSQL access layer"),
    ("Develop the dynamic decision script", "Develop the dynamic decision script"),  # unchanged: passes (modality decides)
    ("Wrote unit test suites for the parser", "Wrote unit tests for the parser"),
    ("Designed and built the release pipeline", "Built the release pipeline"),
    ("Migrated billing to Kubernetes", "Moved billing to Kubernetes"),  # a different verb: a paraphrase, not judged
])
def test_faithful_claims_pass(source, claim):
    assert tense.check(source, claim) is None


def test_a_dropped_negation_is_an_inversion():
    assert tense.check("Did not use Kafka; replaced it with Redis streams", "Used Kafka")[0] == "negation_inverted"
    assert tense.check("Did not use Kafka; replaced it with Redis streams", "Replaced Kafka with Redis streams") is None


# ---- amendments ------------------------------------------------------------------------------------------------------
MANAGED = ("Incoming student requests pass through a semaphore-regulated buffer managed by the OS layer before reaching "
           "MySQL, where row-level locking and ACID transactions guarantee that class seating caps are never exceeded.")


def test_a_passive_participle_in_a_finite_sentence_is_not_a_change():
    """From the first re-run: "a buffer managed by the OS layer" -> "The OS layer manages ..." was a false positive. The
    source has a finite verb ("are"), so the participle is description, not a modality construction."""
    assert tense.judge(MANAGED, "The OS layer manages a semaphore-regulated buffer.")[0] != "rejected"
    # a participle in a source with NO finite verb is a construction: it may not become a finite verb
    assert tense.check("A buffer managed by the OS layer", "The OS layer manages a buffer")[0] == "tense_changed"


@pytest.mark.parametrize("source,claim,inverted", [
    ("Interfacing threading models with SQL commit/rollback workflows without triggering deadlocks.",
     "The workflow does not trigger deadlocks", False),  # one negation each: parity agrees (the re-run's false rejection)
    ("establishing a deterministic baseline first before experimenting with optional AI extensions.",
     "We are not experimenting with optional AI extensions.", True),  # "before" is not a negator
    ("Retries run until the queue drains", "Retries do not run until the queue drains", True),  # nor is "until"
    ("The cache is not unreliable under load", "The cache is reliable under load", False),  # two against none: even
    ("The cache is not unreliable", "The cache is reliable", False),  # the minimal form: no incidental shared word
    ("The cache is unreliable", "The cache is reliable", True),
    # negation scoping (final amendment 1): the two false rejections of the second re-run
    ("Massive user surges slam the portal at identical seconds, requiring front-end throttling instead of direct database "
     "hammering.", "Massive user surges slam the portal at identical seconds.", False),
    ("Interfacing OS-level threading models smoothly with SQL commit/rollback workflows without triggering deadlocks.",
     "Interfacing OS-level threading models smoothly with SQL commit/rollback workflows", False),
    ("The cache is unreliable under load", "The cache is reliable under load", True),
    ("Used Redis rather than Memcached for sessions", "Used Memcached for sessions", True),
    ("The parser fails to handle nested tables", "The parser handles nested tables", True),
])
def test_negation_is_compared_by_parity(source, claim, inverted):
    assert (tense.negation_inverted(source, claim) is not None) is inverted


def test_preventing_to_prevented_is_rejected():
    """Amendment 3, from the re-run: a heading in the gerund became a past-tense accomplishment."""
    assert tense.check("4. Preventing Race Conditions and Overselling", "Prevented race conditions.")[0] == "tense_changed"


@pytest.mark.parametrize("source,claim", [
    # Stage 3 item 3: the two big misses of the lexicon guard, and its lexicon-caused miss
    ("Overall, the system connects admission filters, semaphores, transactions, and storage into a cohesive architecture "
     "designed to handle severe concurrency prior to exploring supplementary machine learning modules.",
     "The system explores supplementary machine learning modules."),
    ("Tuning critical section thresholds required careful experimentation to avoid performance bottlenecks or starvation.",
     "The tuned critical section thresholds avoid performance bottlenecks or starvation."),
    ("Finding the sweet spot between queue length and thread pool sizing proved difficult to prevent CPU saturation.",
     "The sweet spot between queue length and thread pool sizing prevents CPU saturation"),
    ("Task Completed: Deployed worker thread pools alongside synchronization semaphores to regulate execution threads; "
     "Team Member: Bansal, Abhinav.", "Regulated execution threads"),
    ("Migrated billing to Kubernetes to reduce cost", "Reduced cost by migrating billing to Kubernetes"),
    ("We will add retries to the ingest service", "Added retries to the ingest service"),
])
def test_modality_constructions_stated_as_done_are_rejected(source, claim):
    assert tense.check(source, claim)[0] == "tense_changed"


@pytest.mark.parametrize("source,claim", [
    ("Migrated billing to Kubernetes to reduce cost", "Migrated billing to Kubernetes"),
    ("Reduced p95 API latency by 38% by redesigning the PostgreSQL access layer.", "Redesigned the PostgreSQL access layer"),
    ("Interfacing OS-level threading models with SQL commit/rollback workflows without triggering deadlocks.",
     "The workflow does not trigger deadlocks"),
])
def test_faithful_claims_from_modality_sources_pass(source, claim):
    assert tense.check(source, claim) is None


def test_a_source_without_a_construction_is_not_applicable():
    """No verb list: the guard judges only explicit constructions, and says when there is none."""
    assert tense.judge("Our system handles the messy performance spikes.", "The system handles spikes.")[0] == "not_applicable"
    assert tense.judge("Proficient in Python", "Proficient in Python")[0] == "not_applicable"


def test_the_pipeline_records_not_applicable(fake):  # noqa: F811
    out = pipeline.extract_from_text("• Containerised the ledger module for staging", headings=["EXPERIENCE"], decomposition_tier="nano")
    assert [c.tense_guard for c in out.claims] == ["not_applicable"]
    assert out.counters["tense_guard_not_applicable"] == 1
    ok = pipeline.extract_from_text("• Built the ledger module to serve the billing team", headings=["EXPERIENCE"], decomposition_tier="nano")
    assert ok.claims[0].tense_guard == "passed"


# ---- item 1: modality inheritance ------------------------------------------------------------------------------------
def U(uid, text, section, heading_path=None):
    return Unit(uid, text, 0, len(text), section, heading_path if heading_path is not None else ([section] if section else []), "sentence")


def test_a_pending_or_planned_heading_makes_every_unit_beneath_it_a_non_assertion():
    units = [U("a", "Task Pending: Develop the dynamic decision script; Team Member: Dubey, Pragati.", "Tasks Pending (7 pts)"),
             U("b", "Migrate the API gateway", "Roadmap"), U("c", "Ship offline mode", "Future work"),
             U("d", "Add tracing", "Planned features")]
    assert {k: v[0] for k, v in modality.classify(units).items()} == {u.unit_id: "pending_heading" for u in units}


def test_a_future_tense_lead_in_carries_to_its_whole_section():
    sec = "Project Outcome/Deliverables (2 pts)"
    units = [U("a", "Upon completion, ConcurrentReg will deliver a production-ready framework for managing large university "
                    "course registrations.", sec),
             U("b", "Expected outputs include:", sec),
             U("c", "5. An automated concurrency adjuster scaling throughput in response to live load variations.", sec),
             U("d", "Built the queue layer.", "Tasks Completed (7 pts)")]
    got = modality.classify(units)
    assert {k for k in got} == {"a", "b", "c"} and all(v[0] == "future_lead_in" for v in got.values())


def test_a_status_cell_reading_pending_or_in_progress_is_a_non_assertion():
    rows = [NS(text=f"Test Type: {t}; Status (Pass/Fail): {s}; Notes: {n}.", cells=[t, s, n],
               header=["Test Type", "Status (Pass/Fail)", "Notes"])
            for t, s, n in [("High load test", "Pending", "Burst and heavy stress simulations remain scheduled"),
                            ("concurrent request test", "In progress", "Multi-client simulation testing is actively running"),
                            ("Locking test", "Pass", "Row locks prevent conflicts during parallel bookings")]]
    sec = "Testing and Validation Status (2 pts)"
    units = [U(f"r{i}", r.text, sec) for i, r in enumerate(rows)]
    got = modality.classify(units, [NS(row_lines=rows)])
    assert {k: v[0] for k, v in got.items()} == {"r0": "status_cell", "r1": "status_cell"}  # "Pass" is an assertion
    assert "In progress" in got["r1"][1]


def test_status_lines_and_future_statements_are_caught_and_labelled_apart():
    sec = "Deliverables Progress (2 pts)"
    units = [U("a", "Load Benchmarking: Pending for heavy traffic burst scenarios.", sec),
             U("b", "OS Subsystem: Fully built with queue structures, worker threads, and semaphores.", sec),
             U("lead", "Our system, ConcurrentReg, handles the messy performance spikes.", "Project Abstract"),
             U("c", "We plan to benchmark these adaptive models against static thresholds.", "Project Abstract"),
             U("d", "Looking ahead to our adaptive research goals, we are studying MD-ACC methodologies.", "Project Abstract")]
    got = {k: v[0] for k, v in modality.classify(units).items()}
    assert got == {"a": "status_line", "c": "future_statement", "d": "research_activity"}


def test_research_activity_is_its_own_rule_and_says_why():
    """Amendment 4: "we are studying MD-ACC methodologies" is research, not unfinished work: not knowable from code."""
    units = [U("lead", "Our system handles registration spikes.", "Abstract"),
             U("r", "Looking ahead to our adaptive research goals, we are studying MD-ACC methodologies.", "Abstract"),
             U("f", "Moving forward, efforts center on building the adaptive research layer.", "Abstract"),
             U("w", "The adjuster will scale throughput.", "Abstract"),
             U("mid", "The system connects admission filters prior to exploring machine learning modules.", "Abstract")]
    got = modality.classify(units)
    assert got["r"][0] == "research_activity" and "not knowable from code" in got["r"][1]
    assert got["f"][0] == got["w"][0] == "future_statement"
    assert "mid" not in got  # "prior to exploring" inside an assertion is not research activity


@pytest.mark.parametrize("text,section", [
    ("Built a Redis-backed caching layer serving 12k req/s", "Experience"),
    ("Planned and executed the migration to Kubernetes", "Experience"),  # past-tense "planned": done work
    ("Built a futures pricing engine", "Futures Trading Desk"),  # "Futures" is not "Future"
    ("Status: shipped to 40 customers", "Projects"),
    ("Led the release scheduling rewrite", "Experience"),
])
def test_resume_assertions_are_not_non_assertions(text, section):
    assert modality.classify([U("x", text, section)]) == {}


def test_a_non_assertion_never_reaches_a_model_and_is_kept_verbatim(fake):  # noqa: F811
    out = pipeline.extract_from_text("Develop the dynamic decision script that alters system throughput limits automatically.",
                                     headings=["Tasks Pending (7 pts)"], decomposition_tier="nano")
    assert fake.prompts == [] and out.calls == 0 and out.claims == []
    assert [(n.text, n.rule) for n in out.non_assertions] == [
        ("Develop the dynamic decision script that alters system throughput limits automatically.", "pending_heading")]
    assert out.units[0].decisions[0].verdict == "non_assertion"


def test_scope_matches_without_dropping_to_do_not_drop():
    """Scoping must see "without dropping" in the claim's "do not drop" (a suffix stem, no lexicon: dropp -> drop)."""
    assert tense.negation_inverted("Queue buffers handle incoming items without dropping data.",
                                   "Queue buffers do not drop incoming items.") is None
