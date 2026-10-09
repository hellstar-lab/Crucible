"""Every Phase 3 cap and threshold, in one place."""

# GitHub API
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT_S = 20.0
COMMITS_API_MAX_PAGES = 10  # per repo, 100 commits per page, for identity evidence

# Repository selection and cloning
MAX_REPOS = 10
MAX_REPO_MB = 200  # checked against API metadata before cloning, then measured after
CLONE_TIMEOUT_S = 120

# Blame
BLAME_TIMEOUT_S = 30  # per file
MAX_BLAME_FILE_BYTES = 1_000_000  # larger source files are skipped and recorded
MAX_SOURCE_FILES_PER_REPO = 2000  # files beyond this are skipped and recorded
REPO_TIME_BUDGET_S = 300  # blame stops starting new files after this
BLAME_WORKERS = 4

# Unlinked-identity surfacing
MAX_UNMATCHED_AUTHORS = 10  # entries kept per repo, by authored lines
IDENTITY_GAP_SHARE = 0.8  # one unmatched author above this share of source lines

# Repo signals (facts, not judgements)
SINGLE_COMMIT_IMPORT_RATIO = 0.8  # largest commit adds more than this share of current source lines
# History-rewrite signal: committer dates clustered while author dates are spread.
CLUSTER_MIN_AUTHOR_SPAN_DAYS = 7  # below this there is not enough history to judge (-> None)
CLUSTER_SPAN_FRACTION = 0.01  # clustered when committer span / author span < 1%
