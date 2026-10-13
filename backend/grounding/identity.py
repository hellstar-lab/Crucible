"""Phase 4J J2: does the resume's own GitHub identity agree with the one the operator asked us to analyse?

Measured: the clean fixture resume links github.com/jordan-lee-demo, the run analysed hellstar-lab, and nothing said
so: the report presented one account's code as evidence about a different person. This never FAILS a run (an operator
may deliberately analyse another account), but it never proceeds silently either: the result goes into the report
header, a mismatch is shown at the top of the page and on every claim row's authorship cell.

  status      meaning
  agree       the operator's user is one of the GitHub profiles the resume links
  mismatch    the resume links GitHub profile(s) and the operator's user is not among them (mismatch_on "account"),
              or the operator named repositories and NONE is a repository the document links (mismatch_on
              "repository"; measured: an OSDBMS report links KasakRathi/ConcurrentReg, the analysed repository is
              hellstar-lab/Concurrent_Reg, and the account check alone said "unconfirmed", or "agree" under
              --user KasakRathi)
  unconfirmed the resume links repositories but no profile, and none is owned by the operator's user
  no_link     no GitHub link found in the document
Repositories: when the operator gave none and the resume names some, the resume's are used (repos_source "resume").
Deterministic, no network: it compares 2C's extracted links with the operator's input, case-insensitively.
"""

from __future__ import annotations

import re


def resume_github(doc) -> tuple[list[str], list[str]]:
    """(profile accounts, owner/repo targets) from 2C's links, in document order, de-duplicated."""
    accounts, repos = [], []
    for ln in (doc.structure.links if doc is not None and doc.structure else []):
        if ln.kind != "github":
            continue
        if ln.github_target:
            if ln.github_target not in repos:
                repos.append(ln.github_target)
        elif ln.github_account and ln.github_account not in accounts:
            accounts.append(ln.github_account)
    return accounts, repos


def compare(doc, user: str | None, operator_repos: list[str]) -> tuple[dict, list[str]]:
    """(identity_check for the report header, the repositories to analyse)."""
    accounts, rrepos = resume_github(doc)
    repos, source = (list(operator_repos), "operator") if operator_repos else ((rrepos, "resume") if rrepos else ([], "none"))
    u = (user or "").lower()
    owners = sorted({r.split("/")[0] for r in rrepos}, key=str.lower)
    repo_mismatch = bool(rrepos and operator_repos and not ({r.lower() for r in repos} & {r.lower() for r in rrepos}))
    on = None
    if not accounts and not rrepos:
        status, msg = "no_link", "No GitHub link found in the document, so the analysed account could not be checked against it."
    elif accounts and u not in {a.lower() for a in accounts}:
        status, on = "mismatch", "account"
        msg = (f"Identity mismatch: the resume links GitHub {', '.join(accounts)}, but the analysis used {user}. The code "
               f"below is evidence about {user}'s repositories, not about the account the resume names, unless they are "
               "the same person.")
    elif repo_mismatch:
        status, on = "mismatch", "repository"
        loose = lambda r: re.sub(r"[^a-z0-9]", "", r.split("/")[-1].lower())  # noqa: E731
        similar = any(loose(a) == loose(b) for a in repos for b in rrepos)
        msg = (f"Identity mismatch: the document links the repository {', '.join(rrepos)}, but the analysis used "
               f"{', '.join(repos)}" + (" (a similarly named repository under a different owner)" if similar else "")
               + ". The code below is evidence about the analysed repository, not about the one the document names, unless "
               "they are the same project.")
    elif not accounts and u not in {o.lower() for o in owners}:
        status = "unconfirmed"
        msg = (f"The resume links repositories ({', '.join(rrepos)}) but no GitHub profile, and none is owned by {user}; "
               "the analysed account could not be confirmed from the document.")
    else:
        status = "agree"
        msg = f"The resume's GitHub link names {', '.join(accounts or owners)}; the analysis used {user}."
    if source == "resume":
        msg += f" No repository was given by the operator; the repositories the resume names were analysed: {', '.join(repos)}."
    return ({"status": status, "mismatch_on": on, "operator_user": user, "operator_repos": list(operator_repos), "resume_accounts": accounts,
             "resume_repos": rrepos, "repos_analysed": repos, "repos_source": source, "message": msg,
             "source": "2C links compared with the operator's --user / --repo"}, repos)
