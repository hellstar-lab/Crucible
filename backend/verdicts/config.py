"""Phase 4C tiers, escalation margin and cost cap."""

from backend import llm

CONTENT_TIER = "super"
ESCALATION_TIER = "ultra"
# Escalate when the re-ranked probability of the chosen content label is below this (pinned), or when the label is
# "contradicted" (the most consequential output deserves the better model).
ESCALATION_MIN_TOP_PROB = 0.70
MAX_TOKENS = 500
MAX_ITEMS_IN_PROMPT = 10  # 4B already caps at 10
# Per-document cap: about 40 Super judgements at a typical size (1500 tokens in, 300 out). Over it: truncated=True.
SUPER_REFERENCE_CALL_USD = llm.estimate_cost("super", 1500, 300)
DEFAULT_COST_CAP_USD = round(40 * SUPER_REFERENCE_CALL_USD, 6)
