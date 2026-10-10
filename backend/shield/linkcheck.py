"""Display-text vs link-target comparison, shared by the PDF and DOCX paths.

A link whose visible text names one site while its target is another is
deception, not sloppiness ("github.com/jane-doe/payments-api" that opens
evil.example.com). Rules:

  * only flag when BOTH sides contain a parseable host;
  * display text that is not a URL ("my portfolio") is never a finding;
  * the comparison is on the registrable domain, so www. and sub-domains match;
  * a bare domain in display text counts only with a scheme/"www." or a TLD from a
    curated list, so file names such as main.py or README.md are not "hosts";
  * mailto: compares the addresses; Launch / GoToR actions have no host, so any
    URL-looking display text on them is a mismatch.

Nothing here ever fetches or opens a URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Common TLDs. File-extension look-alikes (py, md, rs, sh, pl, so, cc, zip, mov, ps, pm) are
# deliberately absent: "main.py" and "README.md" are not hosts.
TLDS = frozenset(
    "com org net io dev edu gov ai app co me xyz tech info biz in uk de fr us ca au eu nl se no fi es it ch jp cn kr br ru "
    "sg nz ie pt za mx tv gg ly sh_ site online store cloud page link blog wiki news".split()
) - {"sh_"}
_MULTI_SUFFIXES = frozenset(
    "co.uk org.uk ac.uk gov.uk com.au net.au org.au edu.au co.in net.in org.in ac.in co.nz co.jp ne.jp or.jp com.br "
    "com.cn com.mx co.za com.sg com.hk com.tw co.kr".split()
)
_URL_RE = re.compile(
    r"(?i)(?P<scheme>(?:https?|ftp)://|www\.)?(?P<host>(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?P<tld>[a-z]{2,24}))"
    r"(?::\d+)?(?=[/\s?#:,;)\]'\"]|$)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MAX_URI_CHARS = 500


def registrable(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 3:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def display_emails(text: str) -> list[str]:
    return [m.group(0).lower() for m in _EMAIL_RE.finditer(text)]


def display_hosts(text: str) -> list[str]:
    """Hosts that the visible text names, in order, without e-mail domains."""
    text = _EMAIL_RE.sub(" ", text)
    hosts = []
    for m in _URL_RE.finditer(text):
        explicit = bool(m.group("scheme"))
        if explicit or m.group("tld").lower() in TLDS:
            hosts.append(m.group("host").lower())
    return hosts


def uri_host(uri: str) -> str | None:
    try:
        return (urlsplit(uri.strip()).hostname or "").lower() or None
    except ValueError:
        return None


def compare(display_text: str, action: str, target: str | None) -> dict | None:
    """Return mismatch evidence, or None when there is no finding.

    ``action`` is "URI", "Launch", "GoToR" or "Submit"; ``target`` is the URI or
    the file specification (never fetched, never opened)."""
    shown_hosts = display_hosts(display_text)
    shown_mail = display_emails(display_text)
    if not target and action == "URI":
        return None
    tgt = (target or "")[:MAX_URI_CHARS]
    if action == "URI" and tgt.lower().startswith("mailto:"):
        addr = tgt[7:].split("?", 1)[0].strip().lower()
        if shown_mail and addr not in shown_mail:
            return {"action": "URI", "display_email": shown_mail[0], "uri": tgt, "uri_host": None}
        return None
    if action == "URI":
        host = uri_host(tgt)
        if not host or not shown_hosts:
            return None
        if any(registrable(h) == registrable(host) for h in shown_hosts):
            return None
        return {"action": "URI", "display_host": shown_hosts[0], "uri": tgt, "uri_host": host}
    # Launch / GoToR / Submit: the target is a file or form endpoint, so URL-looking display text is a mismatch
    if shown_hosts:
        return {"action": action, "display_host": shown_hosts[0], "uri": tgt, "uri_host": uri_host(tgt)}
    return None


# --- known hosts and GitHub targets (consumed by the Phase 3.6 resolver) -----------------------------
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "linkedin.com": "linkedin", "kaggle.com": "kaggle",
               "huggingface.co": "huggingface", "devpost.com": "devpost"}
_GH_RESERVED = frozenset(
    "settings orgs organizations topics trending sponsors features marketplace login join pricing about explore "
    "notifications issues pulls collections events apps customer-stories enterprise team contact security".split())
_URL_FINDER = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+|\b(?:github|gitlab|linkedin|kaggle|huggingface|devpost)\.(?:com|co)/[^\s<>\"')\]]+")


def classify_host(host: str | None) -> str:
    return KNOWN_HOSTS.get(registrable(host), "other") if host else "other"


def github_target(url: str) -> tuple[str | None, str | None]:
    """(account, "owner/repo" or None) for a github.com URL."""
    if not re.match(r"(?i)^(https?://)?(www\.)?github\.com/", url.strip()):
        return None, None
    path = re.sub(r"(?i)^(https?://)?(www\.)?github\.com/", "", url.strip()).split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0].lower() in _GH_RESERVED or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[0]):
        return None, None
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9._-]+", parts[1]):
        repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        return parts[0], f"{parts[0]}/{repo}"
    return parts[0], None


def find_bare_urls(text: str) -> list[str]:
    out = []
    for m in _URL_FINDER.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if not re.match(r"(?i)https?://", url):
            url = "https://" + url
        out.append(url)
    return out
