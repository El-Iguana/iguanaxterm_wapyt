"""The version, and the check for a newer release (GitHub never called)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services import about, about_service, release_check

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_pyproject():
    """A release bump has to change both, or About shows the wrong version."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"$', pyproject, re.M).group(1)
    assert about.VERSION == declared


@pytest.mark.parametrize("latest, current, newer", [
    ("v2.2.0", "2.1.0", True),
    ("2.1.1", "2.1.0", True),
    ("v3.0", "2.9.9", True),
    ("v2.1.0", "2.1.0", False),
    ("v2.0.9", "2.1.0", False),
    ("v2.10.0", "2.9.0", True),   # numbers, not strings
    ("v2.2.0-rc1", "2.1.0", False),  # pre-releases are not offered
    ("nonsense", "2.1.0", False),
])
def test_is_newer(latest, current, newer):
    assert about.is_newer(latest, current) is newer


@pytest.fixture
def github(monkeypatch):
    """A stand-in for GitHub, counting calls; the cache starts empty."""
    monkeypatch.setattr(release_check, "_cache", {})
    monkeypatch.delenv("GANXTERM_UPDATE_CHECK", raising=False)
    calls = []

    def respond(body):
        def fake():
            calls.append(1)
            if isinstance(body, Exception):
                raise body
            return body
        monkeypatch.setattr(release_check, "_fetch", fake)

    respond.calls = calls
    return respond


def test_a_newer_release_is_reported(github):
    github({"tag_name": "v9.0.0", "html_url": "https://example/rel", "published_at": "2027-01-02T03:04:05Z"})
    result = release_check.latest_release(now=1000)
    assert result == {"current": about.VERSION, "checked": True, "latest": "9.0.0",
                      "newer": True, "url": "https://example/rel", "published": "2027-01-02"}


def test_the_running_release_is_not_newer(github):
    github({"tag_name": f"v{about.VERSION}", "html_url": "u"})
    assert release_check.latest_release(now=1000)["newer"] is False


def test_one_request_serves_everyone_until_it_goes_stale(github):
    github({"tag_name": "v9.0.0"})
    release_check.latest_release(now=1000)
    release_check.latest_release(now=1000 + release_check.CHECK_TTL - 1)
    assert len(github.calls) == 1
    release_check.latest_release(now=1000 + release_check.CHECK_TTL)
    assert len(github.calls) == 2


def test_offline_is_a_quiet_result_retried_sooner(github):
    github(OSError("network unreachable"))
    result = release_check.latest_release(now=1000)
    assert result["checked"] is False and result["reason"] == "unreachable"
    release_check.latest_release(now=1000 + release_check.FAILURE_TTL - 1)
    assert len(github.calls) == 1
    release_check.latest_release(now=1000 + release_check.FAILURE_TTL)
    assert len(github.calls) == 2


def test_an_odd_tag_is_not_trusted(github):
    github({"tag_name": "latest-build"})
    assert release_check.latest_release(now=1000)["checked"] is False


@pytest.mark.parametrize("value", ["off", "0", "false", "NO"])
def test_the_check_can_be_turned_off(github, monkeypatch, value):
    monkeypatch.setenv("GANXTERM_UPDATE_CHECK", value)
    github({"tag_name": "v9.0.0"})
    assert release_check.latest_release() == {"current": about.VERSION, "checked": False, "reason": "off"}
    assert github.calls == []


def test_the_service_needs_a_signed_in_user(github):
    github({"tag_name": "v9.0.0"})
    assert about_service.AboutService().latest()["ok"] is False
