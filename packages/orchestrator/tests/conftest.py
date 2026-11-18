"""Shared fixtures: keep tests environment-independent.

The FR-O28 sandbox availability depends on the host's container runtime
AND the ``qg-sandbox`` image — stub it off by default so suite results
don't change between machines; tests that exercise the sandbox path
re-enable it explicitly via monkeypatch.
"""

import pytest

from pr_review_orch import sandbox


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
