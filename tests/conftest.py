"""Shared test setup.

Fixture PDFs are committed to the repo and are the primary path. This
regenerates any that are missing, as a safety net only.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import os

# Tests never touch the network. The NLI model is pinned by revision and cached locally; without these, transformers
# asks the Hugging Face Hub for metadata on load, and a stalled connection hung the suite (measured in Phase 4J).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURE_DIR))

import make_fixtures  # noqa: E402


def _regenerate_missing() -> None:
    for name in make_fixtures.FIXTURES:
        if not (FIXTURE_DIR / f"{name}.pdf").exists() or not (FIXTURE_DIR / f"{name}.json").exists():
            make_fixtures.build(name)


_regenerate_missing()


import os  # noqa: E402

import pytest  # noqa: E402

from backend import llm  # noqa: E402

# Take the key out of the environment for the whole session, before any
# fixture of any scope runs. Only tests marked ``live`` get it back.
_LIVE_KEY = os.environ.pop("NEBIUS_API_KEY", None)
_LIVE_GITHUB_TOKEN = os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("CRUCIBLE_NONCE_KEY", None)  # tests choose the nonce mode explicitly (monkeypatch); default: random


@pytest.fixture(scope="session", autouse=True)
def _session_call_log(tmp_path_factory):
    """Model call logs from tests go to a temp file, never logs/."""
    llm.MODEL_CALL_LOG = tmp_path_factory.mktemp("logs") / "model_calls.jsonl"
    llm._clients.clear()
    yield
    llm._clients.clear()


@pytest.fixture(autouse=True)
def _offline_unless_live(request, monkeypatch):
    """Offline unless marked ``live``: no key, no cached clients."""
    llm._clients.clear()
    live = request.node.get_closest_marker("live") is not None
    if live and _LIVE_KEY:
        monkeypatch.setenv("NEBIUS_API_KEY", _LIVE_KEY)
    else:
        monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    if live and _LIVE_GITHUB_TOKEN:
        monkeypatch.setenv("GITHUB_TOKEN", _LIVE_GITHUB_TOKEN)
    else:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield
    llm._clients.clear()


@pytest.fixture(scope="session")
def git_fixtures(tmp_path_factory):
    """Synthetic git repositories, generated at test time (nested .git
    directories cannot be committed). Deterministic: see make_git_fixtures."""
    import make_git_fixtures

    return make_git_fixtures.build_all(tmp_path_factory.mktemp("git_repos"))


@pytest.fixture(autouse=True)
def _isolated_evidence_cache(monkeypatch, tmp_path_factory):
    """Tests never read or write the real .cache/ directory."""
    from backend.evidence import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path_factory.mktemp("evidence_cache"))
