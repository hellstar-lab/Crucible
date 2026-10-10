"""Every Phase 3 cap and threshold, in one place."""

# GitHub API
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT_S = 20.0
COMMITS_API_MAX_PAGES = 10  # per repo, 100 commits per page, for identity evidence

# Repository selection and cloning
MAX_REPOS = 10  # account mode
MAX_TARGETS_PER_RUN = 10  # target mode
# The API "size" field is KiB of the PACKED git repository (verified 2026-10-10:
# within 6% of `git count-objects` size-pack on repos of 91 KiB to 4,475 KiB).
# Over this cap a repo is analysed in degraded api_commits_only mode, not skipped.
MAX_REPO_MB = 300
CLONE_TIMEOUT_S = 180
ANALYSIS_TIMEOUT_S = 300
RATE_LIMIT_FLOOR = 10  # stop the batch (and report) below this many remaining calls
COMPARE_FILES_CAP = 300  # GitHub compare returns at most 300 files
DEGRADED_COMMIT_DETAIL_CAP = 100  # per-commit detail calls in api_commits_only mode

# Blame
BLAME_TIMEOUT_S = 30  # per file
MAX_BLAME_FILE_BYTES = 1_000_000  # larger source files are skipped and recorded
MAX_SOURCE_FILES_PER_REPO = 2000  # files beyond this are skipped and recorded
REPO_TIME_BUDGET_S = ANALYSIS_TIMEOUT_S  # blame stops starting new files after this
BLAME_WORKERS = 4

# Unlinked-identity surfacing
MAX_UNMATCHED_AUTHORS = 10  # entries kept per repo, by authored lines
IDENTITY_GAP_SHARE = 0.8  # one unmatched author above this share of source lines

# Repo signals (facts, not judgements)
SINGLE_COMMIT_IMPORT_RATIO = 0.8  # largest commit adds more than this share of current source lines
# History-rewrite signal: committer dates clustered while author dates are spread.
CLUSTER_MIN_AUTHOR_SPAN_DAYS = 7  # below this there is not enough history to judge (-> None)
CLUSTER_SPAN_FRACTION = 0.01  # clustered when committer span / author span < 1%
