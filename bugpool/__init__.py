"""Phase 5: the bug pool. An OFFLINE batch job that searches a repository for pre-validated bug candidates.

    python -m bugpool generate --repo PATH --email ADDR [--email ...] --out pool.json

Nothing here runs during an interview session. The session only reads a pool file (``bugpool.session.plant``), and
plants NOTHING when the pool is empty: "insufficient coverage to plant a validated bug in authored code".

A candidate is in the pool only if ALL of these hold (Phase 5 spike, docs/phase-5-spike.md):
  * the line is in code committed under the candidate's identity (backend/evidence/blame.py), and not excluded
    (backend/evidence/exclusions.py);
  * the mutation is from a whitelist of human-plausible operators (comparison swap within == != < <= > >=, is/is not,
    True/False, inserted or removed `not`, and/or, break/continue, integer +/-1);
  * the line is covered by at least one BEHAVIOUR test (a test touching >= ``behaviour_modules`` distinct modules);
  * running ONLY its covering tests, exactly one test function fails (a clean kill), the failure is not an exception
    raised at the mutated line, and the killing test's name and message do not name the mutated symbol, its condition,
    or a string or exception on an adjacent line (not telegraphed).
Tests are keyed path::function, never a parametrized node id (Q1: a random parameter changes the id every run).

Requires cosmic-ray 8.7.0 and coverage/pytest-cov, which live in a separate environment and are NOT in
requirements.txt; the spike recorded why.

Second tier (planned, NOT built): characterization tests generated for authored code that has no behaviour test, so a
repository with sparse tests can still get a validated bug. ``generate_pool(second_tier=...)`` is the seam.
"""

INSUFFICIENT = "insufficient coverage to plant a validated bug in authored code"
