# Authorship attribution: known limitations

Phase 3 decides which lines of code a candidate personally wrote. Every later
phase builds on that answer, so this page sets out plainly what it can and
cannot see. These limits come from the data available, not from bugs, and a
reviewer reading Crucible's evidence should keep them in mind.

## What the numbers mean

`authored_ratio` is the share of non-blank lines in a repository's **source
files** that `git blame` attributes, at the current HEAD, to an email that
belongs to the candidate. Vendored, generated and documentation files are
excluded, using rules modelled on GitHub Linguist and overridable through
`.gitattributes`, and so are binaries. An email belongs to the candidate only
when one of these supports it:

- GitHub linked commits under that email to the candidate's account;
- it is a GitHub noreply address containing their login;
- it is the email on their public profile.

A repository's `.mailmap` unifies aliases. A matching name is never enough.

## Limitations

**1. Only the candidate's own public repositories are analysed.**
Work contributed as pull requests to other people's projects is invisible to
this phase. A strong engineer whose best work lives in someone else's
repository, such as an open-source maintainer or a contributor to a large
project, will be **under-credited**. A low number here means "little authored
code in their own public repositories", not "little authored code".

**2. Private and enterprise work is invisible.**
Most professional software is written in private repositories. Phase 3
cannot see any of it, so for many experienced engineers it covers only a
small, unrepresentative slice of their work.

**3. Git author email is entirely user-controlled.**
Anyone can commit under any name and email, and a repository's history can
be rewritten (`git filter-branch`, `git filter-repo`) to replace the original
authors with the candidate. `git blame` then credits them with every line,
and GitHub's own attribution agrees, because it trusts the same email.
Phase 3 records a **history-rewrite signal** to make this visible:
`committer_dates_clustered` is true when committer dates fall in a narrow
window while author dates are spread out, which is what a rewrite leaves
behind. It is a **fact, not proof**. Rebases, imports and some migration
tools produce the same pattern for innocent reasons, and a careful forger
can fake committer dates as well. A human must interpret it.

**4. Unlinked git identities are the most common cause of under-attribution.**
Git records whatever email a machine is configured with. If a candidate
commits from a laptop whose `user.email` was never added to their GitHub
account, GitHub does not link those commits to them, and neither do we:
the only connection is a name, and a name is never enough. Their own
repository then reports **0% authored**. That is correct as a measurement,
but it would be misleading if read as "wrote none of this". It means "we
could not link the author". Phase 3 surfaces this instead of guessing:

- `unmatched_authors` lists the authors of surviving source lines who did
  not match. Their emails are **redacted** to the first three characters
  plus the domain (`aur...@gmail.com`), so the candidate can recognise
  their own address without the report holding third parties' full emails.
  Hostname-derived domains (`.local`, `.lan`, `.home`, `.internal`,
  `localhost`, or no dot), which git invents when `user.email` was never
  set, would leak a person's name and device model. They become
  `abh...@<local-machine>`, with the reason "user.email not configured on
  the committing machine", because the cause is what a reader needs. The
  same redaction applies to recorded committer emails.
- `likely_identity_gap` is true when a repository is owned by the
  candidate, is not a fork, is 0% authored, and one unmatched author wrote
  more than 80% of its source lines. That is the exact data shape of "same
  person, unlinked identity". It is a fact about the data, not a
  conclusion, and the CLI prints a note asking a human to verify.

**The fix is on the candidate's GitHub account, not in our code.** Adding the
missing email as a verified address on their GitHub account (Settings →
Emails) makes GitHub link those commits, and Phase 3 then picks them up
automatically through the commits-API signal. Crucible never matches by name
to "help": that would also credit impostors.

**5. Blame credits surviving lines, not historical contribution.**
`git blame` reports who last changed each line that exists today. Early work
that was later refactored, reformatted or rewritten by someone else is
credited to whoever touched it last. Someone who built a project's
foundations and then handed it over can show a low ratio. Equally, a
large-scale reformat can move credit to the person who ran the formatter.

## What is counted (Phase 3.5)

- **Lines.** Every line is classified by pygount as code, documentation
  (comments and docstrings), string or empty. Authorship counts **source
  lines = code + string** only. Comments and blank lines are not evidence of
  engineering. All four counts are recorded per file, so the basis can be
  audited.
- **Files.** Exclusions use GitHub Linguist's own `vendor.yml` and
  `documentation.yml` (vendored and pinned; see `THIRD_PARTY.md`), two of
  Linguist's content heuristics (minified files, source-map references),
  the repository's `.gitattributes` overrides, and pygount's analysis
  states: generated (lock-file names, "do not edit" headers), duplicate
  (byte-identical copies are counted once), binary, empty, and unparseable.
  Every exclusion is reported per repository by reason, in files and lines.

## Known gaps in the exclusion data

Linguist's vendor and documentation data, which we deliberately use instead
of our own lists, does **not** exclude the following. Files like these are
currently **counted as source**:

- **Data files** such as `data/raw/*.json` and CSV exports. pygount counts
  every JSON line as code. In one real repository, 18 JSON files made up 81%
  of its "source" lines.
- **Committed virtualenvs and installed packages:** `venv/`, `.venv/`,
  `site-packages/`.
- **Build output** outside `dist/`, for example `build/` and `target/`.
- **Framework migrations** without a generated-file header.
- **Generated code without a header** that pygount recognises.

Linguist itself handles some of these through other data (language types in
`languages.yml`, and more of `generated.rb`). Adopting that is a separate
decision.

## Smaller caveats

- **Line counting.** See "What is counted" above. Comments are classified
  by pygount's lexers, so an unusual language or an unrecognised file type
  can be misclassified or excluded as unparseable.
- **Caps.** Very large repositories, very large files and slow operations
  are skipped and recorded under caps set in `backend/evidence/config.py`,
  never silently dropped. A skipped repository reports `None` for every
  number, never 0: "not measured" and "measured as zero" are different
  facts. The same applies to a repository with no source lines at all,
  whose ratio is `None`, not 0%.
- **History-rewrite signal needs history.** `committer_dates_clustered` is
  only evaluated when author dates span at least 7 days; below that it is
  `None` with the reason `insufficient_history`.
- **Mixed-content commits.** `single_commit_import` compares the largest
  commit's added source lines with today's source lines. A small repository
  whose code arrived in one commit trips it legitimately.
- **Classification rules.** File classification uses path, extension and
  header rules. Unusual layouts, such as generated code checked in without
  markers or vendored code without a conventional directory name, can be
  misclassified. A repository can correct this with `.gitattributes`.
- **Only the root `.gitattributes` is read.** Nested `.gitattributes` files
  are ignored.
- **Only the default branch is analysed.** Work on other branches is not
  counted.
