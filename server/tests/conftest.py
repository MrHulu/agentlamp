"""Pytest fixtures + path bootstrap for the AgentLamp server tests.

Adds the ``server/`` directory to ``sys.path`` so ``import agentlamp_server``
works without an installed package (the venv has no editable install).
"""
from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


# A fixed, deterministic pepper so HMAC labels are stable and assertable.
TEST_PEPPER = b"agentlamp-test-pepper-32-bytes!!"  # 31 bytes is fine (>0)


@pytest.fixture
def pepper() -> bytes:
    return TEST_PEPPER


@pytest.fixture
def aliases():
    from agentlamp_server import sanitize as S

    return S.AliasMap(
        projects={"/Users/hulu/work/acme": "project-a", "/Users/hulu/side/blog": "project-b"},
        accounts={"default": "main", "work": "work"},
    )


@pytest.fixture
def client():
    """A FastAPI TestClient against a fresh app state."""
    from fastapi.testclient import TestClient
    from agentlamp_server.app import app
    from agentlamp_server.app import _build_state

    # Reset state between tests so seq/sessions do not leak across cases.
    app.state.frame = _build_state()
    return TestClient(app)
