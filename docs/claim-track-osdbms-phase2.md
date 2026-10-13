# Claim track on the OSDBMS phase-II report, against hand-checked ground truth (2026-10-13)

**Inputs**
- **Document:** the phase-II report PDF, `OSDBMS-V-2026-T187` (not committed: four students' personal data).
- **Repository:** `hellstar-lab/Concurrent_Reg` at `f9b6725`.
  - This is the same commit as the local clone; its GitHub HEAD was verified equal.
  - It has one commit, by hellstar-lab.
- **Runs:**
  - `scripts/crucible.py --cost-cap-usd 0.5 --no-questions` with `--user hellstar-lab --email abhinavbansal2120@gmail.com`, the committer: $0.064;
  - the same with `--user KasakRathi --email kasakrrathi@gmail.com`: $0.078.
- **Scorer:** `scripts/spike/q7_phase2_score.py`. Each label maps to claims by a pattern on the claim text.

## Step 1: extraction fixes

**a) x_tolerance.** pdfplumber's default (3) returns the abstract's first line as `Oursystem,ConcurrentReg,handles…`,
and `x_tolerance=2` fixes it. Crucible's page text never used pdfplumber's `extract_text` or `extract_words`: it is
built from pdfium characters, and was already spaced. The tolerance now applies where pdfplumber does build words,
the text-strategy table finder (`PLUMBER_X_TOLERANCE = 2`). A fixture pins both the pdfplumber behaviour and
Crucible's abstract text.

**b) A table with no internal row rules** (`structure.split_stacked`):
- Items are split by vertical gap: on page 3 it is 14.5 pt inside an item and 28.9 pt between items, so the
  threshold is 1.5 line pitches.
- Every other column's lines go to the item whose y-span contains them. Matching tops is never used.
- The section text the claims come from interleaved the columns ("…request dispatcher Rathi, Kasak coupled with…").
  It now has one line per row: "Task Completed: …; Team Member: Rathi, Kasak."
- Pages 3, 4 and 6 split into 4, 5 and 8 rows. Page 3 yields all four pairs.

**Also fixed:**
- **Cell text built by a flat character sort.** It produced "i bl k l kWrote core transact on oc s". Cells now use the
  page's own line builder.
- **Line-end hyphens.** pdfium reports them as U+0002, which was dropped, giving "synchro nization". Words broken at a
  line end are now joined ("synchronization"); a hyphen before a capital is kept ("OS-DBMS").
- Tests: `tests/test_stacked_tables.py` (8). The PDF-based tests skip when the PDF is absent.

## Changes made during this task that bear on the score

Disclosed because they were not blind:
- **Identity banner.** The report links the REPOSITORY `KasakRathi/ConcurrentReg`, not a profile. The account-only
  check said "unconfirmed" (or "agree" under `--user KasakRathi`), and no banner was shown. Reading the ground truth
  led me to add a repository check:
  - when the operator names repositories and none of them is one the document links, the status is `mismatch` with
    `mismatch_on: "repository"`;
  - the banner shows both repositories.

  The "banner raised" result below therefore comes after a fix, and is not an independent pass.
- **The 4A cost cap.** A first run with the default cap ($0.00504, about 40 Nano calls, sized for a one-page résumé)
  processed 13 of 74 units. `--cost-cap-usd` is a new operator flag; the default is unchanged and the value used is
  recorded in the report.

No 4A, 4B or 4C prompt or rule was changed after seeing the per-label results.

## Results, run as the committer (hellstar-lab / abhinavbansal2120)

```

=== reports/creg_phase2_abhinav.json: 78 claims; verdicts {'no_evidence': 14, 'unverifiable': 60, 'inconclusive': 4}

label                                      expect         matched claims (verdict / content-matrix row)
N1 registration stored procedures          not_supported  pass
                                            s3u3.c1   no_evidence   no_evidence                'Designed registration stored procedures'
N2 stored procedure scripts                not_supported  pass
                                            s8u7.c1   unverifiable  gate:retrieval_not_performed 'stored procedure scripts'
N3 'logs' in the MySQL schema              not_supported  pass
                                            s3u3.c0   unverifiable  gate:retrieval_not_performed 'Designed the MySQL relational schema for students, course catalogs, an'
N4 adaptive feedback controller            not_supported  NOT EXTRACTED
N5 monitors ... retry rates                not_supported  NOT EXTRACTED
S1 fixed-capacity buffer queue             supported      fail
                                            s3u0.c1   unverifiable  gate:retrieval_not_performed 'Coupled with a fixed-capacity buffer queue'
S2 worker thread pools                     supported      fail
                                            s2u1.c0   unverifiable  gate:retrieval_not_performed 'Instead of letting every student submission hit the database directly,'
                                            s2u1.c1   unverifiable  gate:retrieval_not_performed 'The inputs are queued, bounded, and processed through thread pools gov'
                                            s3u1.c0   unverifiable  gate:retrieval_not_performed 'Deployed worker thread pools alongside synchronization semaphores'
                                            s4u3.c0   no_evidence   no_evidence:empty_items    'Finding the sweet spot between queue length and thread pool sizing pro'
                                            s5u1.c0   unverifiable  gate:retrieval_not_performed 'Scale our thread pool utilities to support adaptive runtime adjustment'
S3 synchronization semaphores              supported      fail
                                            s1u4.c0   unverifiable  gate:retrieval_not_performed 'Incoming student requests pass through a semaphore-regulated buffer ma'
                                            s1u4.c1   unverifiable  gate:retrieval_not_performed 'The OS layer manages a semaphore-regulated buffer.'
                                            s1u4.c2   unverifiable  gate:retrieval_not_performed 'Requests reach MySQL after passing through the semaphore-regulated buf'
                                            s2u1.c0   unverifiable  gate:retrieval_not_performed 'Instead of letting every student submission hit the database directly,'
                                            s2u1.c1   unverifiable  gate:retrieval_not_performed 'The inputs are queued, bounded, and processed through thread pools gov'
                                            s2u5.c0   unverifiable  gate:retrieval_not_performed 'The system connects admission filters, semaphores, transactions, and s'
                                            s3u1.c0   unverifiable  gate:retrieval_not_performed 'Deployed worker thread pools alongside synchronization semaphores'
                                            s3u1.c1   unverifiable  gate:retrieval_not_performed 'Regulated execution threads using synchronization semaphores'
                                            s9u2.c0   unverifiable  gate:retrieval_not_performed 'Semaphores successfully isolate critical execution regions.'
                                            s9u2.c1   unverifiable  gate:retrieval_not_performed 'Semaphores isolate critical execution regions.'
S4 transaction blocks, lock management     supported      fail
                                            s3u2.c0   inconclusive  inconclusive               'Wrote core transaction blocks, lock management routines, and checks fo'
S5 seat limit and duplicate checks         supported      fail
                                            s1u4.c4   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions guarantee that class seating c'
                                            s3u2.c0   inconclusive  inconclusive               'Wrote core transaction blocks, lock management routines, and checks fo'
                                            s6u5.c0   unverifiable  gate:retrieval_not_performed 'Enforced validation checks stopping duplicate enrollments and seat ove'
S6 row-level locking, ACID                 supported      fail
                                            s1u4.c3   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions are used in MySQL.'
                                            s1u4.c4   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions guarantee that class seating c'
                                            s9u7.c0   unverifiable  gate:retrieval_not_performed 'Row locks prevent conflicts during parallel bookings.'
                                            s9u7.c1   unverifiable  gate:retrieval_not_performed 'Row locks prevent conflicts.'
S7 Python and MySQL stack                  supported      fail
                                            s8u2.c0   inconclusive  supported+confidence_none  'Implementation Stack: Python'
                                            s8u2.c1   unverifiable  gate:retrieval_not_performed 'Implementation Stack: MySQL'
I1 automatic rollback triggers             inconclusive   fail
                                            s9u6.c0   unverifiable  gate:retrieval_not_performed 'Commit and rollback triggers operate as intended.'
I2 High load test: Pending                 inconclusive   fail
                                            s9u3.c0   no_evidence   no_evidence                'The high load test status is pending.'
I3 Load Benchmarking: Pending              inconclusive   NOT EXTRACTED
I4 concurrent request test: In progress    inconclusive   fail
                                            s9u4.c0   no_evidence   no_evidence                'Test Type: concurrent request test; Status (Pass/Fail): In progress'
                                            s9u4.c1   unverifiable  gate:retrieval_not_performed 'Multi-client simulation testing is actively running'
U1 completed our literature review         unverifiable   pass
                                            s7u2.c0   unverifiable  gate:retrieval_not_performed 'We have completed our literature review on OS-DBMS coordination for re'
U2 studying MD-ACC methodologies           unverifiable   fail
                                            s1u6.c0   no_evidence   no_evidence:empty_items    'We plan to benchmark MD-ACC methodologies against static thresholds'
                                            s7u3.c3   unverifiable  gate:retrieval_not_performed 'Referencing MD-ACC literature'

confusion (claims): rows = expected, columns = verdict
                    supported  contradicted  inconclusive   no_evidence  unverifiable
supported                   0             0             3             1            22
not_supported               0             0             0             1             2
inconclusive                0             0             0             2             2
unverifiable                0             0             0             1             2

not in the ground truth: 47 claims; verdicts {'no_evidence': 9, 'unverifiable': 36, 'inconclusive': 2}
  s1u0.c0   no_evidence   metric          'Our system, ConcurrentReg, handles the messy performance spikes that occur when an entire unive'
  s1u0.c1   unverifiable  other           'An entire university logs on simultaneously to enroll in classes.'
  s1u2.c0   inconclusive  ownership       'We built an integrated operating system and database architecture to intercept concurrent reque'
  s1u2.c1   unverifiable  other           'The integrated operating system and database architecture prevents concurrent request floods, d'
  s1u6.c1   unverifiable  other           'under various heavy load profiles'
  s2u0.c0   no_evidence   artifact_exists 'Our design utilizes a layered pipeline separating OS-level thread scheduling from database stor'
  s2u0.c1   no_evidence   artifact_exists 'The layered pipeline separates OS-level thread scheduling from database storage.'
  s2u5.c1   inconclusive  ownership       'The architecture is designed to handle severe concurrency.'
  s2u5.c2   unverifiable  other           'The system explores supplementary machine learning modules.'
  s3u0.c0   no_evidence   ownership       'Built the foundational parallel request dispatcher'
  s4u1.c0   unverifiable  outcome         'Massive user surges slam the portal at identical seconds.'
  s4u1.c1   unverifiable  other           'The portal requires front-end throttling instead of direct database hammering.'
  s4u2.c0   unverifiable  other           'Balancing Queues and Worker Threads'
  s4u5.c0   unverifiable  other           'Tuning critical section thresholds required careful experimentation.'
  s4u5.c1   no_evidence   metric          'The tuning avoided performance bottlenecks or starvation.'
  s4u6.c0   unverifiable  other           'Preventing Race Conditions'
  s4u6.c1   unverifiable  other           'Preventing Overselling'
  s4u7.c0   unverifiable  outcome         'Multiple clients attempting to secure the final seat simultaneously'
  s4u7.c1   unverifiable  other           'Demanded rigorous transaction locking'
  s4u9.c0   unverifiable  technology_use  'Interfacing OS-level threading models smoothly with SQL commit/rollback workflows'
  s4u9.c1   unverifiable  other           'The workflows do not trigger deadlocks'
  s5u0.c0   unverifiable  other           'Finalize the core admission control logic'
  s5u0.c1   unverifiable  other           'Link the core admission control logic securely to the request queue buffer'
  s5u1.c1   unverifiable  other           'The adaptive runtime adjustments are based on OS telemetry inputs'
  s5u2.c0   no_evidence   artifact_exists 'Execute deep database stress tests targeting deadlocks, contention spikes, and fully booked cou'
  s5u2.c1   unverifiable  other           'Setting up telemetry loggers'
  s5u3.c0   no_evidence   metric          'Developed the dynamic decision script that alters system throughput limits automatically using '
  s5u4.c0   unverifiable  other           'Integrate the adaptive engine across both layers'
  s5u4.c1   unverifiable  other           'Run full benchmarking suites'
  s6u0.c0   unverifiable  other           'Upon completion, ConcurrentReg will deliver a production-ready framework'
  s6u0.c1   unverifiable  other           'The production-ready framework will be used for managing large university course registrations'
  s6u4.c0   unverifiable  other           'Clean architectural middleware links operating system controls to the database layer.'
  s6u4.c1   unverifiable  other           'The clean architectural middleware links operating system controls to the database layer.'
  s6u6.c0   no_evidence   metric          'An automated concurrency adjuster scales throughput.'
  s6u6.c1   unverifiable  other           'The automated concurrency adjuster responds to live load variations.'
  s7u3.c0   unverifiable  other           'Building the adaptive research layer'
  s7u3.c1   unverifiable  other           'Including admission control rules'
  s7u3.c2   unverifiable  other           'Including runtime monitors'
  s7u4.c0   unverifiable  other           'We are keeping strictly to our staged schedule: establishing a deterministic baseline first.'
  s7u4.c1   unverifiable  other           'We are not experimenting with optional AI extensions.'
  s8u5.c0   unverifiable  other           'Core OS request routing scripts.'
  s8u5.c1   unverifiable  other           'Core OS buffering scripts.'
  s8u7.c0   unverifiable  technology_use  'MySQL database schema design'
  s9u0.c0   unverifiable  other           'Incoming portal requests route properly'
  s9u0.c1   unverifiable  other           'Incoming portal requests ingest properly'
  s9u6.c1   no_evidence   artifact_exists 'The transaction test passes.'
  s10u3.c0  unverifiable  other           'Adaptive Control Layer is under development for automated workload throttling.'
```

## Results, run as kasakrrathi

```

=== reports/creg_phase2_kasak.json: 76 claims; verdicts {'no_evidence': 11, 'unverifiable': 57, 'inconclusive': 8}

label                                      expect         matched claims (verdict / content-matrix row)
N1 registration stored procedures          not_supported  pass
                                            s3u3.c1   inconclusive  contradicted:any->gate:scope 'Wrote registration stored procedures'
N2 stored procedure scripts                not_supported  pass
                                            s8u7.c1   unverifiable  gate:retrieval_not_performed 'stored procedure scripts'
N3 'logs' in the MySQL schema              not_supported  pass
                                            s3u3.c0   unverifiable  gate:retrieval_not_performed 'Designed the MySQL relational schema for students, course catalogs, an'
N4 adaptive feedback controller            not_supported  NOT EXTRACTED
N5 monitors ... retry rates                not_supported  NOT EXTRACTED
S1 fixed-capacity buffer queue             supported      fail
                                            s3u0.c1   unverifiable  gate:retrieval_not_performed 'Coupled with a fixed-capacity buffer queue'
S2 worker thread pools                     supported      fail
                                            s2u1.c0   unverifiable  gate:retrieval_not_performed 'Instead of letting every student submission hit the database directly,'
                                            s2u1.c1   unverifiable  gate:retrieval_not_performed 'inputs are processed through thread pools governed by custom semaphore'
                                            s3u1.c0   unverifiable  gate:retrieval_not_performed 'Deployed worker thread pools alongside synchronization semaphores'
                                            s5u1.c0   unverifiable  gate:retrieval_not_performed 'Scale our thread pool utilities to support adaptive runtime adjustment'
S3 synchronization semaphores              supported      fail
                                            s1u4.c0   unverifiable  gate:retrieval_not_performed 'Incoming student requests pass through a semaphore-regulated buffer ma'
                                            s1u4.c1   unverifiable  gate:retrieval_not_performed 'The OS layer manages the semaphore-regulated buffer.'
                                            s1u4.c2   unverifiable  gate:retrieval_not_performed 'The semaphore-regulated buffer passes requests to MySQL.'
                                            s2u1.c1   unverifiable  gate:retrieval_not_performed 'inputs are processed through thread pools governed by custom semaphore'
                                            s2u5.c0   unverifiable  gate:retrieval_not_performed 'The system connects admission filters, semaphores, transactions, and s'
                                            s3u1.c0   unverifiable  gate:retrieval_not_performed 'Deployed worker thread pools alongside synchronization semaphores'
                                            s9u2.c0   unverifiable  gate:retrieval_not_performed 'The semaphores successfully isolate critical execution regions.'
                                            s10u0.c2  unverifiable  gate:retrieval_not_performed 'OS Subsystem uses semaphores'
S4 transaction blocks, lock management     supported      fail
                                            s3u2.c0   no_evidence   no_evidence                'Wrote core transaction blocks'
                                            s3u2.c1   unverifiable  gate:retrieval_not_performed 'Lock management routines'
S5 seat limit and duplicate checks         supported      fail
                                            s1u4.c4   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions guarantee that class seating c'
                                            s3u2.c2   unverifiable  gate:retrieval_not_performed 'Checks for seat limits'
                                            s3u2.c3   unverifiable  gate:retrieval_not_performed 'Checks for duplicate entries'
                                            s6u5.c0   unverifiable  gate:retrieval_not_performed 'Enforced validation checks stopping duplicate enrollments'
                                            s6u5.c1   unverifiable  gate:retrieval_not_performed 'Enforced validation checks stopping seat overbooking errors'
S6 row-level locking, ACID                 supported      fail
                                            s1u4.c3   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions are used in MySQL.'
                                            s1u4.c4   unverifiable  gate:retrieval_not_performed 'Row-level locking and ACID transactions guarantee that class seating c'
                                            s9u7.c0   unverifiable  gate:retrieval_not_performed 'Row locks prevent conflicts during parallel bookings.'
S7 Python and MySQL stack                  supported      fail
                                            s8u2.c0   inconclusive  supported+confidence_none  'Implementation Stack: Python'
                                            s8u2.c1   unverifiable  gate:retrieval_not_performed 'Implementation Stack: MySQL'
I1 automatic rollback triggers             inconclusive   fail
                                            s9u6.c0   unverifiable  gate:retrieval_not_performed 'Commit and rollback triggers operate as intended'
I2 High load test: Pending                 inconclusive   fail
                                            s9u3.c0   no_evidence   no_evidence                'Test Type: High load test'
I3 Load Benchmarking: Pending              inconclusive   NOT EXTRACTED
I4 concurrent request test: In progress    inconclusive   fail
                                            s9u4.c0   unverifiable  gate:retrieval_not_performed 'Multi-client simulation testing is actively running.'
                                            s9u4.c1   no_evidence   no_evidence                'Test Type: concurrent request test; Status (Pass/Fail): In progress.'
U1 completed our literature review         unverifiable   NOT EXTRACTED
U2 studying MD-ACC methodologies           unverifiable   pass
                                            s1u5.c0   unverifiable  gate:retrieval_not_performed 'Studying MD-ACC methodologies'

confusion (claims): rows = expected, columns = verdict
                    supported  contradicted  inconclusive   no_evidence  unverifiable
supported                   0             0             1             1            23
not_supported               0             0             1             0             2
inconclusive                0             0             0             2             2
unverifiable                0             0             0             0             1

not in the ground truth: 46 claims; verdicts {'no_evidence': 8, 'unverifiable': 32, 'inconclusive': 6}
  s1u0.c0   no_evidence   metric          'Our system, ConcurrentReg, handles the messy performance spikes that occur when an entire unive'
  s1u5.c1   unverifiable  other           'Transitioning from fixed limits to real-time, telemetry-driven concurrency control'
  s2u0.c0   no_evidence   artifact_exists 'Our design utilizes a layered pipeline separating OS-level thread scheduling from database stor'
  s2u0.c1   no_evidence   artifact_exists 'The layered pipeline separates OS-level thread scheduling from database storage.'
  s2u5.c1   inconclusive  ownership       'The cohesive architecture is designed to handle severe concurrency.'
  s2u5.c2   unverifiable  other           'The system explores supplementary machine learning modules.'
  s3u0.c0   inconclusive  ownership       'Built the foundational parallel request dispatcher'
  s3u1.c1   unverifiable  other           'Regulated execution threads'
  s3u2.c4   unverifiable  role_or_team    'Team Member: Shah, Yogesh'
  s4u1.c0   unverifiable  outcome         'Massive user surges slam the portal at identical seconds'
  s4u1.c1   unverifiable  other           'Require front-end throttling instead of direct database hammering'
  s4u5.c0   unverifiable  other           'Tuning critical section thresholds required careful experimentation.'
  s4u5.c1   no_evidence   metric          'The careful experimentation avoided performance bottlenecks or starvation.'
  s4u9.c0   unverifiable  technology_use  'Interfacing OS-level threading models smoothly with SQL commit/rollback workflows'
  s4u9.c1   unverifiable  other           'The workflows do not trigger deadlocks'
  s5u2.c0   inconclusive  artifact_exists 'Execute deep database stress tests targeting deadlocks, contention spikes, and fully booked cou'
  s5u2.c1   unverifiable  other           'Set up telemetry loggers'
  s5u3.c0   no_evidence   metric          'Developed the dynamic decision script that alters system throughput limits automatically using '
  s5u3.c1   no_evidence   metric          'The dynamic decision script alters system throughput limits automatically using live performanc'
  s5u4.c0   unverifiable  other           'Integrate the adaptive engine across both layers'
  s5u4.c1   unverifiable  other           'Run full benchmarking suites under burst and normal loads'
  s6u0.c0   unverifiable  other           'ConcurrentReg will deliver a production-ready framework'
  s6u0.c1   unverifiable  other           'The framework is production-ready'
  s6u4.c0   unverifiable  other           'Clean architectural middleware links operating system controls to the database layer.'
  s6u4.c1   unverifiable  other           'Clean architectural middleware links the database layer to operating system controls.'
  s6u6.c0   no_evidence   metric          'An automated concurrency adjuster scales throughput in response to live load variations.'
  s6u6.c1   no_evidence   metric          'The automated concurrency adjuster scales throughput.'
  s7u1.c0   unverifiable  other           'Our primary OS and database foundation is functional.'
  s7u1.c1   unverifiable  other           'Our primary OS and database foundation supports safe multi-threaded transactions.'
  s7u3.c0   unverifiable  other           'Building the adaptive research layer'
  s7u3.c1   unverifiable  other           'Including admission control rules'
  s7u3.c2   unverifiable  other           'Including runtime monitors'
  s8u5.c0   unverifiable  other           'Core OS request routing scripts.'
  s8u5.c1   unverifiable  other           'Core OS buffering scripts.'
  s8u7.c0   unverifiable  technology_use  'MySQL database schema design'
  s9u0.c0   unverifiable  other           'Incoming portal requests route properly'
  s9u0.c1   unverifiable  other           'Incoming portal requests ingest properly'
  s9u1.c0   unverifiable  other           'Queue buffers handle incoming items without dropping data.'
  s9u1.c1   unverifiable  other           'Queue buffers handle incoming items.'
  s9u1.c2   unverifiable  other           'Queue buffers do not drop data.'
  s9u2.c1   inconclusive  artifact_exists 'The test passed.'
  s9u3.c1   unverifiable  other           'Status (Pass/Fail): Pending'
  s9u6.c1   inconclusive  artifact_exists 'The test passes'
  s10u0.c0  inconclusive  ownership       'OS Subsystem is built with queue structures'
  s10u0.c1  unverifiable  other           'OS Subsystem has worker threads'
  s10u3.c0  unverifiable  other           'Adaptive Control Layer is under development for automated workload throttling.'
```

## Reading the score

**What passes, and why it is weak**
- **No false CONTRADICTED anywhere,** in either run. All four canaries are clear of the failure the operator named.
- **The identity banner is raised in both runs,** after the fix disclosed above.
- **The MUST-NOT-be-supported labels pass, but vacuously:**
  - nothing in either run is supported;
  - two of the five (the adaptive feedback controller, retry rates) were never extracted: their unit opens "Building
    upon this, …" and 4A refused it as `cannot_be_disambiguated` in both runs.

**What fails, by cause**
1. **0 supported, out of 25 to 26 claims that should be.**
   - **Not searched:** about 22 of them were never searched at all. 4A gave them kind `other` ("Deployed worker
     thread pools alongside synchronization semaphores", "Coupled with a fixed-capacity buffer queue"), and 4B has no
     checkability rule for `other`, so they come back unverifiable.
   - **MySQL is not in the technology lexicon,** so "Implementation Stack: MySQL" and "MySQL database schema design"
     are unverifiable.
   - **Searched, but capped:** where the content was found, the single-commit rule caps the verdict at inconclusive
     (`supported+confidence_none`), as designed: blame cannot say who wrote a one-commit upload.
2. **Canaries come back no_evidence or unverifiable, not inconclusive.**
   - "High load test: Pending" and "concurrent request test: In progress" become `artifact_exists` claims, and
     nothing is found.
   - "Commit and rollback triggers operate as intended" is `other`, so it is unverifiable.
   - "Load Benchmarking: Pending" was dropped as `fabricated_assertion`.
   - The page-2 sentence holding "automatic rollback triggers" was dropped as `cannot_be_disambiguated`.
3. **MD-ACC.** "We plan to benchmark MD-ACC methodologies…" was searched as an artifact and came back no_evidence.
   "Studying MD-ACC methodologies" itself was dropped in the committer run.

**Interesting disagreements among the unlabelled claims**
- **Pending tasks rewritten as done.** "Develop the dynamic decision script…" (Tasks Pending) became the claim
  "Developed the dynamic decision script…", which is no_evidence. 4A changed the tense of a pending task into an
  accomplishment.
- **Expected deliverables stated as current facts.** "An automated concurrency adjuster scales throughput." is a
  numbered item under "Upon completion … Expected outputs include". It came back no_evidence where it should be
  unverifiable as a plan.
- **A paraphrase that inverts the meaning.** "…before experimenting with optional AI extensions" became "We are not
  experimenting with optional AI extensions."
- **A possible false negative.** "The transaction test passes." is no_evidence, although the repository has
  commit/rollback tests (`tests/test_registration.py`). The claim names a status, not a file.
- **Plausibly supportable, but no_evidence.** "Our design utilizes a layered pipeline separating OS-level thread
  scheduling from database storage." The repository does separate `app/os_layer/` from the database code.

## Step 3: the team problem (no team mode built)

**Ownership rows differ between the two runs, but not because of the identity.**
- Authorship confidence is `none` in both runs, from the single-commit import. `authored_by_candidate` is null on
  every row, so neither identity was credited with any code.
- The differences between runs are 4A and 4C variance:
  - "Built the foundational parallel request dispatcher" is no_evidence in one run and inconclusive in the other;
  - "Wrote core transaction blocks…" is inconclusive in one run and no_evidence in the other, where it was split
    into four claims;
  - the stored-procedures claim is no_evidence in the committer run and inconclusive in the run as Kasak
    (`contradicted:any->gate:scope`).

**Attributed to the wrong person: yes, at extraction, in both runs.**
- 2C now hands 4A "Task Completed: X; Team Member: Rathi, Kasak". 4A drops the team member from every task claim and
  states the task as the candidate's own. The run as Kasak once emitted "Team Member: Shah, Yogesh" as a separate
  role claim, detached from its task.
- **Run as the committer:** 5 of the 7 task claims belong to someone else (Kasak 2, Yogesh 1, Pragati 2). They appear
  in his report as his claims.
- **Run as Kasak:** 9 of the 11 task claims belong to someone else.
- **No verdict credited the wrong person,** only because the single-commit rule refused all authorship. With a real
  multi-author history, the committer's run would have checked Kasak's dispatcher claim against Abhinav's code.

# Stage 1: stop claim extraction fabricating (2026-10-13)

**Modules**
- `backend/claims/modality.py` (item 1);
- `backend/claims/tense.py` (item 2);
- `backend/claims/attribution.py` (item 3);
- the coverage record, the scaled cap and the partial-analysis banner (item 4).

New tests: `test_claims_modality.py`, `test_claims_attribution.py` and `test_coverage_banner.py`. Full suite: 918
passed, 1 skipped.

**Order inside 4A, all deterministic and all before any model call:**
1. `names_another_person`;
2. then modality, as a non-assertion kept verbatim;
3. then the model stages, with the tense guard on every Stage 4 claim.

So a pending row that names someone else is listed as another person's work, not as "not yet done".

**Beyond the stated rules, reported separately so they can be vetoed:**
- `status_line`: a "Label: Pending / Under development …" line, treated as a borderless table row;
- `future_statement`: a unit that itself says "we plan to", "looking ahead", "moving forward" or "will …".

**The tense guard was checked against 93 recorded résumé claims before this run:**
- it first rejected 3, all the same faithful decomposition ("by redesigning" became "Redesigned");
- a gerund now takes the tense of the main verb it hangs from, and the count is 0 of 93.

## Re-run: same PDF, `f9b6725`, candidate abhinavbansal2120

**Totals:**
- cost $0.0456;
- 4A cap $0.07056, scaled for 74 units (the old fixed cap was $0.00504);
- coverage: 74 of 74 units, 39 of 39 claims judged, so no banner.

**The lists:**
- **Stated as not yet done (19):**
  - pending_heading 2: Abhinav's own pending task and "All team members";
  - future_lead_in 8: the whole Deliverables section;
  - status_cell 2: High load test, concurrent request test;
  - status_line 4;
  - future_statement 3: MD-ACC studying, "we plan to benchmark", "moving forward".
- **Names another person (6):** Tasks Completed for Kasak, Yogesh and Pragati, and Tasks Pending for Kasak, Yogesh
  and Pragati.
- **Rejected by the tense guard (2), both false:**
  - "…a semaphore-regulated buffer managed by the OS layer…" became "The OS layer manages a semaphore-regulated
    buffer." The source's "managed by" is a passive participle, not the past tense.
  - "…without triggering deadlocks." became "The workflow does not trigger deadlocks". "without" is already a
    negation, so the polarity is unchanged.
- **Missed by the tense guard (1):** "4. Preventing Race Conditions and Overselling" became "Prevented race
  conditions." "prevent" is not in the verb lexicon, so the guard could not judge it.
- **The three original fixtures did not reach Stage 4 live:**
  - "Develop the dynamic decision script" is Pragati's row, so it was excluded as another person's work;
  - the adjuster is in the future-tense section, so it is a non-assertion;
  - "We are keeping strictly to our staged schedule…" was refused at Stage 3 this run (run-to-run variance).

  The unit tests cover all three.

## Stage 1 amendments, and the second re-run (2026-10-13)

**Applied**
1. A passive participle ("managed by") takes the tense of its source's main verb.
2. Negation is compared by **parity**.
   - Lexicon: not, n't, never, no, without, lacks, fails to, absent, rather than, instead of. "before" and "until" are
     NOT negators.
   - An un-/non- word counts as a negation when the other side has the same word without the prefix, so "not
     unreliable" against "reliable" is even, and passes.
   - The source side is the clause with the most content words in common with the claim.
3. "prevent" is judged, along with a guard-local list of other verbs; `verbs.py`, used by the M2 guard, is unchanged.
   A claim the guard cannot judge is marked `unjudged_by_guard` with the word: `verb_not_in_list`, `no_verb`, or
   `verb_not_in_source` (a paraphrase). It is never silently passed. The count and the words are on the report.
4. `research_activity` (studying / exploring / investigating / researching, with a subject) is split from
   `future_statement`. The report lists it apart as "not knowable from code".
5. `names_unresolved`: when the candidate's name cannot be found in the document, a row naming a person is never
   assumed to be the candidate's. It is excluded with the name shown, and the report says so. Groups ("All team
   members") are kept.

Replay of the 93 recorded résumé claims: 0 rejected.

Full suite: 933 passed, 1 skipped.

**Re-run: same PDF, `f9b6725`, candidate Abhinav ($0.040)**
- Coverage: 74 of 74 units, 36 of 36 claims judged.
- Research activity: 1 (MD-ACC studying).
- Future statements: 2.

**The guard fired 4 times:**

| Claim | Source | Fired | Verdict on the firing |
|---|---|---|---|
| "Regulated execution threads" | "Deployed worker thread pools … to regulate execution threads" | tense_changed | correct by the rule: a purpose ("to regulate") stated as done |
| "The tuning avoided performance bottlenecks or starvation." | "… required careful experimentation to avoid performance bottlenecks …" | tense_changed | correct: a purpose stated as an outcome |
| "Massive user surges slam the portal at identical seconds." | "…, requiring front-end throttling instead of direct database hammering." | negation_inverted | **false**: the "instead of" belongs to a phrase the claim did not take |
| "Interfacing OS-level threading models smoothly with SQL commit/rollback workflows" | "… without triggering deadlocks." | negation_inverted | **false**: the claim drops the negated phrase; it does not invert it |

**Unjudged: 7.**
- `no_verb` 5: "Implementation Stack: Python", "Implementation Stack: MySQL", "MySQL database schema design", and two
  that DO have a verb ("Our system … handles …", "… foundation supports …"). "handle" and "support" are on verbs.py's
  noun-like list, so their -s forms are read as nouns and the label is wrong.
- `verb_not_in_list` 2: "is" (a copula) and "located".

**The three original fixtures still did not reach the guard:**
- Develop …: excluded as names_another_person (Pragati's row);
- the adjuster: a non-assertion (future_lead_in);
- "before experimenting": excluded at Stage 3 (cannot_be_disambiguated) for the second run running.

## Stage 1 final amendments, and Stage 2 (2026-10-13)

**Stage 1, final**

**1. Negation scoping.** A source negation counts only when its scope words appear in the claim.
- *Exact-word scoping* passed the "not unreliable under load" test only through the incidental word "load". It FAILED
  the minimal "The cache is not unreliable" → "The cache is reliable".
- *Fix, applied:* scope words match on lemma candidates, and a un-/non- word is attached to its own root. The clause
  finder also attaches the prefix, without which the minimal inversion "unreliable" → "reliable" found no clause and
  was missed entirely.
- *Regressions caught on the way, before any run:*
  - "without dropping" vs "do not drop" (a suffix stripper gave `dropp`);
  - "supporting" under the copula "is", fixed by letting a copula be the main verb a participle inherits from.

**2. "handles" and "supports" are read as verbs.** The corrected unjudged count on the second re-run's claims is 5
(was 7), with no new rejections.

**3. `unjudged_by_guard` never blocks.** Test `test_unjudged_label_value_claims_reach_retrieval_and_verification`:
the three label-value claims are searched and judged.

**4. FEEDBACK.md #15.** Recorded per firing. The false positives were 2 truncations and 2 rewordings, not 4
truncations.

**Stage 2**
- **`r_artifact`.** It searches the code content for concepts listed in `data/code_concepts.json`, excluding tests,
  and cites the enclosing function. A claim that would route to `other` but names a concept now routes to `ownership`
  if it states building, else to `artifact_exists`. Both kinds run `r_artifact`. The ownership provenance gate is
  unchanged, so an `r_artifact` citation cannot contradict ownership.
- **MySQL** is in the technology lexicon: drivers, imports and connection URLs.
- **Test-status rows** (a status cell reading Pass / Fail …) get a 4C gate, `gate:test_status`: inconclusive, no model
  call, with the reason that a run result is not recorded in a repository.
- Tests: `test_artifact_retrieval.py` (11). Full suite: 950 passed, 1 skipped.

**Third re-run: same PDF, `f9b6725`, candidate Abhinav ($0.054)**
- **Coverage:** 74 of 74 units, 37 of 37 claims judged.
- **Expected supported:** inconclusive 11, no_evidence 1, unverifiable 1. Before Stage 2 it was 3 / 1 / 22: they are
  now searched, and capped at inconclusive by the single-commit rule. There are still 0 supported and 0 contradicted.
- **The scorer** now shows where an unmatched label went:
  - names_another_person: N1, N3, S1, S4;
  - non-assertion: I2, I3, I4;
  - Stage 3 cannot_be_disambiguated: N4, N5, I1, U2. U2 is also a non-assertion.
- **The guard fired 4 times:**

  | Claim | Verdict on the firing |
  |---|---|
  | "Regulated execution threads" | correct |
  | "Prevented race conditions and overselling" (from the heading "Preventing …") | correct; this construction's first live firing |
  | "Preventing CPU saturation." (from "… to prevent CPU saturation") | false: a gerund fragment asserts nothing |
  | "The queue buffers handle incoming items without dropping data." | **correct block, wrong reason**: the content is the NEXT row's, a cross-row bleed, not a negation change (relabelled, Stage 2 follow-ups) |

- **Found, not fixed:**
  1. **Five of the six "Pass" test-status rows never reach 4C.** The unit begins "Test Type: …", the M2 verbless-source
     guard reads "Test" as verb_ambiguous, so it treats the unit as verbless and drops every claim as
     fabricated_assertion.
  2. **"…a buffer managed by the OS layer" routes to role_or_team.** "managed" matches the role lexicon, so the claim is
     unverifiable.


## Stage 2 follow-ups (2026-10-13)

**A. M2 on table rows.**
- A claim from a reconstructed row is judged against the FULL row, value cells included
  (`verbs.fabricated_assertion_in_row`). A verb counts as added only if its lemma appears nowhere in the row.
- "Status: Pass" therefore holds "passes". A bare auxiliary ("are isolated") is not an added action.
- The row format is unchanged and there is no "Label:" special case. Every other unit keeps M2 as it was.

**B. The bleed was not in the table reconstruction.**
- Each reconstructed row holds only its own cells (checked on the PDF).
- The text crossed in 4A: every row unit carried its sibling rows as "Before/After" context, and the model built the
  Request-handling row's claim from the next row's notes.
- Fix: `pipeline.isolate_rows`. A row's context keeps its headings, never its sibling rows.
- Fixtures: a synthetic table (no sibling row in any row's prompts) and the phase-II PDF.
- The guard firing is relabelled above as a correct block with the wrong reason.

**C. Gerund fragments.**
- A unit or a Stage 4 claim that is a gerund phrase with no subject and no finite verb becomes a non-assertion
  (`gerund_fragment`), before the tense guard.
- Examples: "Preventing CPU saturation.", "Handling High-Concurrency Inflows".
- **Why M2 did not take it:** M2 asks whether the SOURCE is verbless, judging its first word, and the source has
  "proved". It also asks whether the CLAIM asserts a verb from M2's lexicon, and "prevent" is not in it.
- **Found on the re-run and fixed:** "Incoming portal requests route properly" was read as a fragment. "Incoming" is an
  adjective, so the first word must now be a known verb in its -ing form.

**D. Role words** count only for a person or team agent. "a buffer managed by the OS layer" routes to artifact_exists.
One condition: a role word followed by "by <determiner> <agent>" whose agent names no person or team.

Tests: `test_stage2_followups.py`. Full suite: 970 passed (before the D/C fix-up), and 64 in the touched files after.

**Re-runs: same PDF, `f9b6725`, candidate Abhinav (4th $0.040, 5th $0.056)**
- **Pass/Fail rows reaching 4C:** 5 of 6 in each run, and a DIFFERENT row was lost each time.
  - 4th run: Request handling, read as a gerund fragment (fixed).
  - 5th run: Database operation, whose claim "Status (Pass/Fail): Pass." failed the existing entity check: "Pass."
    with its trailing period is not "Pass;" in the source.

  Every row that reached 4C came back inconclusive (`gate:test_status`), and nothing was contradicted in either run.
- **Guard firings (5th run):** 1, "The tuning avoided …", correct.
  - 4th run: 2, "Regulated …" and "… avoided …", both correct.
  - With the reclassification, every live firing since Stage 2 is either correct or a correct block for the wrong
    reason.
- **Guard miss (4th run):** "The sweet spot … prevents CPU saturation" from "… to prevent CPU saturation". The guard
  took the noun "queue" for the claim's main verb.
- **Confusion (5th run):** expected supported → 16 inconclusive, 3 no_evidence; not_supported → 1 no_evidence;
  canaries → 1 inconclusive; unverifiable → 1 unverifiable.

**The four canaries, explicitly (5th run):**

| Canary | Where it went | Verdict |
|---|---|---|
| I1 "automatic rollback triggers" | Transaction-test row "Commit and rollback triggers operate as intended." reached 4C. The Approach-section sentence that says "automatic rollback triggers" was dropped at Stage 3 (cannot_be_disambiguated) in every run, so it has still never been judged | **inconclusive** (`gate:test_status`) for the row |
| I2 "High load test: Pending" | non-assertion (status_cell) | none: never verified |
| I3 "Load Benchmarking: Pending" | non-assertion (status_line) | none: never verified |
| I4 "concurrent request test: In progress" | non-assertion (status_cell) | none: never verified |

None was contradicted. Only I1 was judged at all, and only through the table row. I2–I4 carry no verdict, by the
Stage 1 modality rule.

## Stage 2 close-out (2026-10-13)

**1. Test-status rows are deterministic.**
- Extraction keeps each row verbatim as one claim (`decomposition_tier: deterministic`, no model call).
- 4C's `gate:test_status` judges it: always inconclusive, same reason, no model.
- All six rows, every run (test: `test_every_pass_row_is_one_verbatim_claim_with_no_model_call`).
- The entity normalisation defect is fixed independently in `support.entities`: trailing sentence punctuation is no
  longer part of a word, so "Pass." equals "Pass;". Internal dots stay ("Node.js", "ASP.NET"), and a real invented
  entity is still caught.

**2. FEEDBACK #16:** a claim class with exactly one possible verdict gets no model call.

**3. Neighbour contamination, the bullet path.**
- `isolate_siblings`: a bullet's context keeps its headings, its parent bullet, its own sub-bullets, the other sentences
  of the same bullet and non-bullet lines (a list's lead-in). It drops every other item of the list.
- 19 of 21 bullets in the phase-II PDF and 3 of 3 in `resume_standard.pdf` change context.
- **Did any bullet-sourced claim change?** 4A only, old against new, two runs each. A change counts only if it held in
  both runs of one condition and in neither run of the other.
  - `resume_standard.pdf`: NO change. The runs differ only in wording noise (2 claims within each condition).
  - Phase-II PDF: within-condition noise is 7–8 claims, so only these are attributable:
    - **"Core OS request routing and buffering scripts."** (a commit-milestone bullet) gave 2 claims with sibling
      context. Without it, Stage 2 calls the unit no_verifiable_claims in both runs.
    - "Finding the sweet spot … proved difficult to prevent CPU saturation." (the claim as written) appears only with
      siblings.
    - "The tuned critical section thresholds avoid performance bottlenecks or starvation." appears only without
      siblings. It is a purpose ("to avoid") stated as an outcome, and the tense guard PASSES it: "avoid" keeps its base
      form, so the comparison sees no change. That is a guard miss, recorded under item 5.
  - Cost of the comparison: about $0.12.

**4. Ground-truth correction: I2–I4 are MODALITY canaries.**
- The original label for "High load test: Pending", "Load Benchmarking: Pending" and "concurrent request test: In
  progress" was "must be inconclusive, not contradicted". That label predates modality inheritance (Stage 1).
- Under it, a pending item is a NON-ASSERTION: listed as "stated as not yet done", never retrieved, never given a
  verdict. That is better than the label asked for. An inconclusive verdict still implies the work was looked for as if
  claimed done; a non-assertion says the document never claimed it.
- The corrected canary: I2–I4 must be marked stated-as-not-yet-done and must NEVER receive a verdict. On every run
  since Stage 1 they have been. I1 ("automatic rollback triggers") is the one canary that still needs a verdict (see
  Stage 3).

**5. The tense guard: count, then decide.**

All five runs that had the guard (`creg_phase2_stage1`, `_stage1b`, `_stage2`, `_stage2b`, `_stage2c`). Misses were
found with a scan that needs no verb lexicon: a verb in a "to + VERB", "will / plan to + VERB" or opening "VERBing"
construction of the source, appearing in a finite form in the claim.

| | Firings / occurrences | Distinct sentences |
|---|---|---|
| Correct catches (all purpose → outcome: "Regulated" ×3, "avoided" ×3, "Prevented" ×1) | 7 | 3 |
| Correct block, wrong reason (the cross-row bleed) | 1 | 1 |
| False positives | 5 | 5 |
| Verb-lexicon-caused errors (see the breakdown below) | 4 | 3 |
| Other misses: "The system explores …" from "prior to exploring …" (4 runs) and "… avoid …" from "to avoid" (A/B runs) | 4 + 2 | 2 |

The five false positives:
- "managed by" (participle);
- "without" → "not" (negation word);
- "instead of" (truncation);
- "without triggering" (truncation);
- "Preventing CPU saturation." (a fragment).

The four verb-lexicon-caused errors:
- "Prevented race conditions." and "Prevented overselling.": misses, because "prevent" was not in the list;
- "… prevents CPU saturation": a miss, because the noun "queue" was taken as the main verb;
- "managed by": a false positive the missing "pass" contributed to.

**Call: keep the guard and its lexicon; do not narrow it now.** Lexicon-caused errors (4 occurrences, 3 sentences) do
not outnumber correct catches (7 and 3). They tie on distinct sentences, which is not "outnumber".

Recorded, not acted on: the largest miss class is STRUCTURAL, not lexical.
- "prior to exploring" inherits the main verb's tense as if it were a participle.
- "to avoid" → "avoid" keeps the base form, so nothing looks changed.

Both are explicit modality-bearing constructions, which the narrowed design (to + VERB, a bare VERBing fragment, a
passive participle, will / plan) compares directly against the claim's finite verb. If the criterion were all errors
rather than lexicon-caused errors (5 false positives + 10 misses against 7 catches), the call would flip.

## Stage 3: what it refuses, and why (report only; nothing changed)

**Across the five runs, Stage 3 (disambiguation) refused 11 distinct units.**
- **5 are refused in 5 of 5 runs:**
  - s1u2 "We built an integrated … architecture …";
  - **s2u2, the "automatic rollback triggers" sentence (canary I1);**
  - s2u3, the adaptive feedback controller / retry rates sentence (N4, N5);
  - s2u4 "This draws heavily from MD-ACC research patterns.";
  - **s7u4, fixture 3 ("… before experimenting with optional AI extensions").**
- **10 of the 11 are `resolutions disagreed`:** the Stage 3 ensemble (n=3, temperature 0.7, UNANIMITY required)
  produced samples that did not all agree. Only s2u4 is a genuine unresolved referent: "MD-ACC" is an unexplained
  acronym, refused unanimously.

**Why so many.**
- **The pre-gate:** Stage 3 runs whenever a unit contains a pronoun or demonstrative (`stages.needs_disambiguation`):
  it, they, **we, our**, this, these, … A team progress report uses "we" and "our" in almost every sentence.
- **The vote:** the three samples must give the same rewrite (equal after normalisation, or the same added words and
  mutual entailment). A long sentence with several such words is rewritten three different ways.

**The actual samples for the two sentences.** These were replayed with the current code (`stage3_probe.py` in the
scratchpad, Ultra, $0.004). They are representative of, not identical to, the five live runs, which do not store
samples.

*s2u2:* "Once cleared through these OS filters, queries reach our MySQL instance where transactions execute under
strict isolation with automatic rollback triggers for race conditions or full classes." Pre-gate trigger: "these".
- **Sample 0, resolved, but by inventing referents:** "… these OS filters (custom semaphores and thread pool
  governance) … our MySQL instance (database storage) where transactions (student submissions) execute …".
  "transactions (student submissions)" is wrong.
- **Sample 1, resolved differently:** "Once cleared through these OS-level thread scheduling and queuing mechanisms,
  queries reach our MySQL database storage instance …".
- **Sample 2, refused:** failed referent "these OS filters": "not defined in the current unit, the headings, or the
  surrounding context".
- Verdict: `resolutions disagreed`. Unanimity correctly stopped sample 0's fabricated referent from shipping. But the
  checkable content ("transactions … with automatic rollback triggers") does not depend on what "these OS filters"
  refers to, so the whole sentence was lost to a referent in a subordinate clause.

*s7u4:* "We are keeping strictly to our staged schedule: establishing a deterministic baseline first before
experimenting with optional AI extensions." Pre-gate trigger: "We".
- **Samples 0 and 2, resolved, and IDENTICAL:** the unit unchanged, "no pronouns or ambiguous references requiring
  resolution. 'Our' clearly refers to the document author's team".
- **Sample 1, refused:** failed referent "our staged schedule": "the specific schedule is not explicitly defined in
  the provided context".
- Verdict: `resolutions disagreed`. Two of three samples said there is nothing to resolve, and one dissent on a
  first-person phrase refuses the unit.

**What this points at (for the next step, not done).**
1. **First-person author references.** "we", "our" and "us" in a document written by the candidate (or the
   candidate's team) refer to the author. Triggering Stage 3 on them invites a refusal with nothing to resolve
   (s7u4).
2. **Unanimity over a whole long sentence.** A referent the claim does not depend on still sinks it (s2u2). The
   unanimity rule itself is doing its job: sample 0 fabricated.

## Stage 3: pronouns, fail-open, the narrowed guard (2026-10-13)

**1. First person never triggers Stage 3.**
- "we / us / our / ours / I / me / my / mine" are out of the pre-gate (`stages._PRONOUN`).
- Safe: authorship is decided by git blame, so resolving a pronoun cannot credit anyone with anything.
- Of the 11 units Stage 3 had refused, this alone clears **4**: s2u0, s7u1, s7u4 (fixture 3) and s8u0. They no longer
  reach Stage 3 at all.

**2. Fail open, filter by containment.** When the samples DISAGREE:
- every resolution is discarded, and its text in the audit record is replaced by a hash;
- the original sentence is decomposed;
- a claim still containing an unresolved span (a refusing sample's failed referent, a demonstrative with its noun
  phrase, or the bare demonstrative) is dropped as `unresolved_referent`;
- the other claims are kept.

A unanimous refusal is still a refusal (MD-ACC). Fixture (FakeLLM, the three live samples): the rollback sentence
yields "Transactions execute under strict isolation with automatic rollback triggers …" and drops the "these OS filters"
claim. "transactions (student submissions)" appears nowhere: not in a claim, a prompt, or the extraction record. (That
invented resolution was the first sample, index 0, not sample 1.)

**3. The narrowed tense guard, no verb lexicon.**
- **Constructions it matches:** to + VERB (and to + VERBing), a bare VERBing fragment, a passive participle in a
  source with no finite verb, and will / plan to / intends to. Verbs are matched by suffix stem; there is no list.
- **Present participles are NOT a construction.** Including them, which went beyond the request, produced 7 false
  positives over the five recorded runs and no catch: "a pipeline separating" → "separates", "…, requiring" →
  "requires". Without a lexicon the source's own finite verb ("utilizes", "slam") is invisible.
- **The negation-parity check is unchanged, now lexicon-free.**
- **Claims whose source has no construction** are `not_applicable` (was `unjudged_by_guard`). They are still never
  blocked.

**Replay over the five recorded runs** (every claim the guard saw, kept or rejected):

| | Lexicon guard | Narrowed guard |
|---|---|---|
| Correct catches | 7 firings, 3 sentences | **18 firings, 5 sentences** |
| Correct block, wrong reason (cross-row bleed, negation) | 1 | 1 |
| False positives | 5 | **0** |
| Misses (lexicon-free scan) | 10 | **0** |

- Both big misses are caught: "prior to exploring" → "explores" (4 runs) and "to avoid" → "avoid".
- The three earlier catches stay caught: "Regulated", "avoided", "Prevented".
- Newly caught: "Synchronization semaphores regulate execution threads" (from "to regulate"), and "prevents CPU
  saturation".
- 93 recorded résumé claims: 0 rejected (85 not_applicable, 8 passed).
- Fixtures the guard no longer catches, both handled upstream by modality:
  - "Develop the dynamic decision script" (a bare imperative under Tasks Pending, so a non-assertion);
  - "An automated concurrency adjuster scaling throughput" (a present participle under a future-tense lead-in, so a
    non-assertion).

Full suite: 985 passed, 1 skipped.

**4. Re-run: same PDF, `f9b6725`, candidate Abhinav ($0.044)**
- **Coverage:** 74 of 74 units, 48 of 48 claims judged. That is more claims than any run before (36–42), because units
  Stage 3 refused now yield claims.
- **Stage 3 refusals: 2 (were 5–8 per run).**
  - s1u2, "…intercept this traffic upstream before it overwhelms…": the samples disagreed, it failed open, and every
    claim still contained "this" or "it", so it was refused. This is the containment rule working.
  - s2u4, MD-ACC: refused unanimously, a real unresolved acronym.
- **The guard fired 8 times:**
  - 4 tense_changed, all correct: "scales concurrency bounds" (from "to scale"), "explores", "prevents CPU saturation",
    "avoid performance bottlenecks";
  - 1 negation_inverted, correct: **fixture 3, "We are not experimenting with optional AI extensions", reaching the
    guard live for the first time** now that first person no longer sends it to Stage 3;
  - 3 unresolved_referent, from fail-open containment.
- **Confusion:** expected supported → 10 inconclusive, 10 no_evidence; not_supported → 4 no_evidence, 2 unverifiable;
  canaries → 1 inconclusive; unverifiable → 1. Nothing is contradicted and nothing is supported.

  The no_evidence rise is newly extracted claims, mostly from the abstract's s1u4 ("Row-level locking in MySQL
  guarantees that class seating caps are never exceeded"). They are judged now, and nothing evidences a guarantee.
  The table counts a claim once per label it matches.

**The four canaries:**

| Canary | Status |
|---|---|
| I1 "automatic rollback triggers" | **Its own sentence (s2u2) passed Stage 3 this run, then was dropped at decomposition by M2.** The verbless-source guard's lexicon (`verbs.py`) knows none of "cleared / reach / execute / trigger", so it reads the 25-word sentence as verbless, and the claim "Automatic rollback triggers are in place for race conditions." as adding a verb. I1 is judged only through the Transaction-test row, inconclusive (`gate:test_status`). **I1's own sentence has still never been judged.** |
| I2 High load test: Pending | stated as not yet done (status_cell), no verdict ✓ |
| I3 Load Benchmarking: Pending | stated as not yet done (status_line), no verdict ✓ |
| I4 concurrent request test: In progress | stated as not yet done (status_cell), no verdict ✓ |
