"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Let Home Assistant load custom_components/automation_advisor. The recorder's database
    fixture must be prepared before `hass` exists, so resolve it first when a test needs it."""
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_db_url")
    request.getfixturevalue("enable_custom_integrations")


@pytest.fixture
def config_dir(hass, tmp_path):
    """A writable config dir whose configuration.yaml includes automations.yaml, like a
    default Home Assistant install."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("automation: !include automations.yaml\n")
    (tmp_path / "automations.yaml").write_text("[]\n")
    (tmp_path / ".storage").mkdir()
    return tmp_path
