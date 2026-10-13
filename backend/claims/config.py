"""Phase 4A caps and limits, in one place."""

from backend import llm

STAGE_MAX_TOKENS = 600  # thinking is off, so this budget is for the JSON answer alone

# Per-document cost cap. Default: the cost of ~40 Nano stage calls at a typical size (900 tokens in, 300 out).
# Over the cap the result is PARTIAL with truncated=True; the units not reached are listed, never dropped silently.
NANO_REFERENCE_CALL_USD = llm.estimate_cost("nano", 900, 300)
DEFAULT_COST_CAP_USD = round(40 * NANO_REFERENCE_CALL_USD, 6)
# Stage 1 item 4: the default cap scales with document size. A unit costs up to three stage calls, Stage 3 on Ultra
# (~3.3x Nano); measured on the 70-unit OSDBMS phase-II report: about $0.00064 per unit, ~5 Nano-call equivalents.
# 8 per unit leaves headroom; never below the old 40-call floor. Measured: the fixed cap analysed 13 of 74 units.
PER_UNIT_NANO_CALLS = 8


def cost_cap_for(n_units: int) -> float:
    return round(max(40, PER_UNIT_NANO_CALLS * n_units) * NANO_REFERENCE_CALL_USD, 6)

# Stage 3 tier: ULTRA (Phase 4A close-out, C1). Measured with the n=3 ensemble (P2-3), over w3 and the two units
# wrongly refused in round 1, 2 repeats each: unanimous 1/6 nano, 5/6 super, 6/6 ultra; correct 3/6, 5/6, 6/6.
# PROVISIONAL on near-zero volume: after the pre-gate Stage 3 almost never fires (0 calls on both fixtures), so the
# 3.3x price matters little. Revisit if Stage 3 starts firing often. Ultra's temperature-0 determinism (FEEDBACK #13)
# is NOT the reason: it does not transfer to the temperature-0.7 sampling the ensemble uses.
# Earlier history (T2): provisional on n=3, one case (w3 "Improved it by 40%"): nano chose cannot_be_disambiguated
# 2/5 and GUESSED a referent 3/5 (one guess was a rubric example); super 3/3 and ultra 3/3 clean. Super, not ultra:
# the same result at about 1/3.3 of the price. The S2 pre-gate made Stage 3 nearly free (0 calls on both
# fixtures), so nano's price advantage no longer matters. The frozen eval set confirms or overturns this.
# Ultra is wired as an escalation (stage3_escalation="ultra") when a guard catches super's rewrite; off by default.
STAGE3_TIER = "ultra"  # C1: see the note above; supersedes the provisional "super"
# C2: reason before verdict is the DEFAULT for every 4A stage schema and the 4C judgement schema (see stages.py).
REASON_FIRST = True

# P2-3: Stage 3 as an ensemble (one request, n samples, unanimity). n <= 3 and n > 1 needs temperature > 0 (measured).
STAGE3_ENSEMBLE_N = 3
STAGE3_ENSEMBLE_TEMPERATURE = 0.7
STAGE3_ESCALATION_TIER = "ultra"  # now the same as STAGE3_TIER, so escalation is a no-op unless the tier changes

FRAGMENT_MIN_CHARS = 8  # a Shield hidden fragment at least this long is searched for in the units (defence in depth)

# Every example string quoted in a stage rubric, registered as DATA (T3/T5). Two uses:
#  * the rubric-leak guard: an output carrying a distinctive n-gram from one of these that the source does not
#    contain was copied from the prompt (measured twice: "Engineered a Kafka-based event bus handling 5k events/s"
#    for an unrelated role line, and "the command-line tool for log search" as a guessed Stage 3 referent);
#  * the prompt/eval overlap test: no prompt may contain a string that appears in the eval set.
# tests/test_claims_prompt_hygiene.py fails if a rubric quotes a string that is not registered here.
RUBRIC_EXAMPLES: dict[str, list[str]] = {
    "selection": ["Detail-oriented and a fast learner", "Strong communicator"],
    "disambiguation": [
        "Analyst, Fabrikam Logistics Sep 2016 - May 2019",  # was the eval row w3 context (T5)
        "Ported it to TypeScript", "Wrote a command-line tool for log search", "it",
        "Moved the service to three regions", "Payments API", "the service",
        "Its error rate fell below 1%", "Built the invoice export job", "its",
    ],
    "decomposition": [
        "Built a Kafka-based event bus handling 5k events/s", "Built a Kafka-based event bus", "The Kafka-based event bus handles 5k events/s",
        "Cut build time 50% by caching Docker layers", "Cut build time by 50%", "Cached Docker layers in the build", "by ...",
        "Moved logging to Loki and Grafana", "Moved logging to Loki", "Moved logging to Grafana",
        "Raised the cache hit rate to 90% on the search path", "on the search path",
    ],
}
LEAK_NGRAM = 4  # content words in a row that make an example phrase distinctive

# T4: words that turn "Python" into a different hiring claim ("Proficient in Python"). Closed list, no threshold:
# such a word in an output but not in the source unit is an ungrounded qualifier. Morphological variants included.
UNGROUNDED_QUALIFIERS = frozenset("""
proficient proficiency expert expertise advanced extensive extensively senior deep deeply strong strongly
skilled seasoned highly mastery fluent fluency accomplished
guarantee always never fully
""".split())
# The guarantee words (claim track close-out): a claim that ADDS "guarantees / never / always / fully" to a source that
# does not say it is excluded here; one whose source does say it reaches 4C's gate:guarantee_over_mechanism.
