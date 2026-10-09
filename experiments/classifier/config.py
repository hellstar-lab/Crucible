"""NOT IN THE SHIPPING PATH. Negative result — see docs/classifier-experiment.md.

Thresholds for the classifier experiment (moved out of backend/shield/config.py).
"""

CLASSIFY_MAX_CHARS = 2000  # fragments longer than this are truncated before classification
CLASSIFY_MAX_TOKENS = 60  # safe only with thinking disabled (see FEEDBACK.md)
MAX_CLASSIFIED_FRAGMENTS = 50  # beyond this, fragments are marked classification_failed unclassified

# Keyword heuristic used by fallback.py when the model is unreachable.
ATTACK_KEYWORDS = (
    "ignore",
    "disregard",
    "previous instructions",
    "prior instructions",
    "system prompt",
    "you are",
    "you must",
    "as an ai",
    "language model",
    "assistant",
    "rate this",
    "rank this",
    "score this",
    "recommend",
    "pre-approved",
    "hire this",
    "override",
    "instruction",
)
