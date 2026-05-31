"""Fixtures + path bootstrap for the collector tests.

Puts ``<repo>/src`` and ``<repo>/server`` on sys.path so ``import collector`` and
``import agentlamp_server`` both work without an installed package, and points the
collector's local state at a throwaway tmp dir.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parents[1]            # <repo>/src
_REPO = _TESTS_DIR.parents[2]               # <repo>
_SERVER_DIR = _REPO / "server"
for _p in (str(_SRC_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TEST_PEPPER = b"agentlamp-test-pepper-32-bytes!!"


@pytest.fixture
def pepper() -> bytes:
    return TEST_PEPPER


@pytest.fixture
def aliases():
    from agentlamp_server import sanitize as S

    return S.AliasMap(
        projects={"/Users/hulu/work/acme": "project-a"},
        accounts={"default": "main"},
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Each test gets a private queue / dead_letter / config dir under tmp."""
    qd = tmp_path / "queue"
    dl = tmp_path / "dead_letter"
    cfg = tmp_path / "config"
    monkeypatch.setenv("AGENTLAMP_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTLAMP_QUEUE_DIR", str(qd))
    monkeypatch.setenv("AGENTLAMP_DEAD_LETTER_DIR", str(dl))
    monkeypatch.setenv("AGENTLAMP_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AGENTLAMP_PEPPER_HEX", TEST_PEPPER.hex())
    # Reload config so the env overrides take effect for modules that read at import.
    import importlib

    import collector.config as cfgmod

    importlib.reload(cfgmod)
    yield tmp_path
