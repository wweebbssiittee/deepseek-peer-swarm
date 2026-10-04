"""Shared test setup.

Pricing depends on the time of day, so tests must not read the wall clock.
Every test runs inside DeepSeek's peak window unless it says otherwise, which
keeps the published peak rates as the expected values throughout the suite.
"""

import pytest

import swarm.billing


@pytest.fixture(autouse=True)
def isolated_provider_credentials(monkeypatch):
    """Tests must never load a developer's configured environment credentials."""
    for index in range(1, 11):
        monkeypatch.delenv(f"DEEPSEEK_API_KEY_{index}", raising=False)


@pytest.fixture(autouse=True)
def peak_pricing(request, monkeypatch):
    if "offpeak" in request.keywords:
        monkeypatch.setattr(swarm.billing, "is_peak", lambda moment=None: False)
    elif "real_clock" not in request.keywords:
        monkeypatch.setattr(swarm.billing, "is_peak", lambda moment=None: True)
