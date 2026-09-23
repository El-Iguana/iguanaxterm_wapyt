"""
Which SSH failures are worth retrying, and which must never be.

The app opens several connections to the same host in quick succession — the
interactive SFTP pool, the transfer pool, one per terminal — and a server with a
tight MaxStartups or a rate limiter resets some of them during the banner
exchange. Those are worth retrying.

Authentication and host-key failures are not: retrying bad credentials burns
attempts against the very limiter that is refusing us and can trip a lockout,
and retrying a changed host key would paper over the one failure that might
mean an interception.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import paramiko
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services.ssh import HostKeyChanged, SSHUnavailable, _is_transient, connect  # noqa: E402


RETRYABLE = [
    # The exact error a folder upload produced against a real host.
    paramiko.SSHException(
        "Error reading SSH protocol banner[Errno 104] Connection reset by peer"
    ),
    ConnectionResetError(104, "Connection reset by peer"),
    ConnectionRefusedError(111, "Connection refused"),
    paramiko.SSHException("EOF during negotiation"),
    socket.timeout("timed out"),
    EOFError(),
]

FATAL = [
    paramiko.AuthenticationException("Authentication failed."),
    HostKeyChanged("example.com", "ssh-ed25519 AAA", "ssh-ed25519 BBB"),
    paramiko.SSHException("not a valid RSA private key file"),
    paramiko.SSHException("Unrecognised private key format"),
]


@pytest.mark.parametrize("error", RETRYABLE, ids=lambda e: type(e).__name__ + str(e)[:28])
def test_transient_failures_are_retried(error):
    assert _is_transient(error) is True


@pytest.mark.parametrize("error", FATAL, ids=lambda e: type(e).__name__ + str(e)[:28])
def test_configuration_failures_are_not_retried(error):
    assert _is_transient(error) is False


def _profile() -> dict:
    return {
        "id": 1, "host": "example.invalid", "port": 22, "username": "u",
        "password": "p", "private_key": "", "host_key": "",
    }


def test_retries_then_reports_a_readable_error(monkeypatch):
    """A persistent transient failure ends as SSHUnavailable, not paramiko text."""
    calls = []

    def fake_connect(self, **kwargs):
        calls.append(kwargs)
        raise paramiko.SSHException(
            "Error reading SSH protocol banner[Errno 104] Connection reset by peer"
        )

    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect)
    monkeypatch.setattr("services.ssh.time.sleep", lambda _s: None)

    with pytest.raises(SSHUnavailable) as caught:
        connect(_profile(), attempts=3)

    assert len(calls) == 3, "should have used every attempt"
    message = str(caught.value)
    assert "example.invalid" in message
    assert "3 attempts" in message
    # The raw paramiko wording must not reach the user.
    assert "Errno 104" not in message


def test_a_later_attempt_can_succeed(monkeypatch):
    """The first two resets are absorbed; the third attempt connects."""
    attempts = {"n": 0}

    def flaky_connect(self, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise paramiko.SSHException("Error reading SSH protocol banner")
        return None

    monkeypatch.setattr(paramiko.SSHClient, "connect", flaky_connect)
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", lambda self: None)
    monkeypatch.setattr("services.ssh.time.sleep", lambda _s: None)

    client = connect(_profile(), attempts=3)
    assert client is not None
    assert attempts["n"] == 3


def test_authentication_failure_is_not_retried(monkeypatch):
    """One attempt only — retrying bad credentials can trip a lockout."""
    calls = []

    def refuse(self, **kwargs):
        calls.append(1)
        raise paramiko.AuthenticationException("Authentication failed.")

    monkeypatch.setattr(paramiko.SSHClient, "connect", refuse)
    monkeypatch.setattr("services.ssh.time.sleep", lambda _s: None)

    with pytest.raises(paramiko.AuthenticationException):
        connect(_profile(), attempts=3)
    assert len(calls) == 1


def test_changed_host_key_is_not_retried(monkeypatch):
    """A changed host key is a security decision, never a transient glitch."""
    calls = []

    def changed(self, **kwargs):
        calls.append(1)
        raise HostKeyChanged("example.invalid", "ssh-ed25519 AAA", "ssh-ed25519 BBB")

    monkeypatch.setattr(paramiko.SSHClient, "connect", changed)
    monkeypatch.setattr("services.ssh.time.sleep", lambda _s: None)

    with pytest.raises(HostKeyChanged):
        connect(_profile(), attempts=3)
    assert len(calls) == 1
