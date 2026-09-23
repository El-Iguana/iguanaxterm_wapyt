"""
Paramiko connection helpers: key loading and host-key verification.

Two things the original got wrong and this fixes:

* only RSA private keys loaded, so an Ed25519 or ECDSA key — the default for
  ``ssh-keygen`` since 2021 — failed with a confusing parse error;
* ``AutoAddPolicy``, which accepts any host key silently, on every connect.
"""
from __future__ import annotations

import base64
import io
import logging
import socket
import threading
import time
from typing import Optional

import paramiko

logger = logging.getLogger("iguanaxterm.ssh")

# Ordered by how likely a modern key is to be one of these. DSSKey was dropped
# in Paramiko 5, so it is resolved by name rather than imported — DSA keys are
# long deprecated but an old saved profile may still carry one.
_KEY_CLASSES = tuple(
    key_class
    for key_class in (
        getattr(paramiko, name, None)
        for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")
    )
    if key_class is not None
)


class HostKeyChanged(Exception):
    """The host presented a different key than the one we pinned."""

    def __init__(self, host: str, expected: str, received: str) -> None:
        super().__init__(
            f"Host key for {host} has changed. Expected {expected}, got {received}. "
            "This is either a server rebuild or a machine-in-the-middle; clear the "
            "pinned key from the session profile only if you know which."
        )
        self.host = host
        self.expected = expected
        self.received = received


def load_private_key(pem: str, passphrase: str = "") -> paramiko.PKey:
    """
    Parse a PEM private key of any type Paramiko supports.

    Raises:
        paramiko.SSHException: none of the key types matched, or the passphrase
            is wrong. The passphrase case is reported distinctly, because
            "not a valid key" for a key that is merely locked wastes real time.
        """
    last_error: Optional[Exception] = None
    needs_passphrase = False

    for key_class in _KEY_CLASSES:
        handle = io.StringIO(pem)
        try:
            return key_class.from_private_key(handle, password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            needs_passphrase = True
            last_error = exc
        except paramiko.SSHException as exc:
            last_error = exc

    if needs_passphrase:
        raise paramiko.SSHException(
            "This private key is encrypted and needs its passphrase."
        )
    raise paramiko.SSHException(
        f"Unrecognised private key format ({last_error})."
    )


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style ``SHA256:base64`` fingerprint."""
    digest = base64.b64encode(key.get_fingerprint()).decode().rstrip("=")
    return f"MD5:{digest}" if len(digest) < 22 else f"SHA256:{digest}"


def serialize_host_key(key: paramiko.PKey) -> str:
    """``<type> <base64>`` — the authorized_keys form, minus the comment."""
    return f"{key.get_name()} {key.get_base64()}"


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """
    Trust-on-first-use host key checking.

    The first connection records the host key on the session profile. Every
    later connection must match it, or the connection is refused. This is the
    same model as OpenSSH's ``known_hosts`` with ``StrictHostKeyChecking=ask``,
    minus the prompt: the pin lives on the profile row rather than in a shared
    file, so two users saving the same host each pin it independently.
    """

    def __init__(self, expected: str = "", on_learn=None) -> None:
        self.expected = (expected or "").strip()
        self._on_learn = on_learn
        self.learned: Optional[str] = None

    def missing_host_key(self, client, hostname, key) -> None:
        received = serialize_host_key(key)
        if not self.expected:
            self.learned = received
            if self._on_learn is not None:
                self._on_learn(received)
            return
        if received != self.expected:
            raise HostKeyChanged(hostname, self.expected, received)


def build_connect_kwargs(session: dict, timeout: int = 15) -> dict:
    """
    Paramiko ``connect()`` arguments for a decrypted session profile.

    Note there is no ``keepalive`` key: ``SSHClient.connect()`` has no such
    parameter. Keepalive is set on the transport after the connection is up.
    """
    kwargs: dict = {
        "hostname": session["host"],
        "port": int(session["port"] or 22),
        "username": session["username"],
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        # Only the credentials on the profile are used: never the server
        # account's own agent or ~/.ssh keys, which would let any saved profile
        # borrow the host's identity.
        "allow_agent": False,
        "look_for_keys": False,
    }

    private_key = (session.get("private_key") or "").strip()
    if private_key:
        kwargs["pkey"] = load_private_key(private_key, session.get("passphrase", ""))
    else:
        kwargs["password"] = session.get("password") or ""
    return kwargs


class SSHUnavailable(Exception):
    """The host refused or dropped the connection, after retries."""


def _is_transient(error: BaseException) -> bool:
    """
    True for the failures that mean "try again", not "this is misconfigured".

    An SSH server under MaxStartups pressure, or behind a rate limiter, resets
    the socket during the banner exchange. Paramiko surfaces that as a generic
    SSHException whose message embeds the errno, so the text has to be matched.
    Authentication and host-key failures are deliberately excluded — retrying
    those just burns attempts against a limiter and can trip lockouts.
    """
    if isinstance(error, (ConnectionResetError, ConnectionRefusedError, EOFError)):
        return True
    if isinstance(error, (paramiko.AuthenticationException, HostKeyChanged)):
        return False
    if isinstance(error, paramiko.SSHException):
        text = str(error).lower()
        return any(
            marker in text
            for marker in ("banner", "reset by peer", "connection reset",
                           "eof during negotiation", "timed out")
        )
    return isinstance(error, (socket.timeout, TimeoutError, OSError))


def connect(
    session: dict,
    timeout: int = 15,
    on_learn_host_key=None,
    attempts: int = 3,
) -> paramiko.SSHClient:
    """
    Open an authenticated SSH connection with host-key pinning and keepalive.

    Retries transient failures. The app opens several connections to the same
    host in quick succession — the interactive SFTP pool, the transfer pool,
    one per terminal — and a server with a tight MaxStartups or a rate limiter
    resets some of them during the banner exchange. That is exactly the case
    where trying again works, so it is not worth surfacing to the user.

    Args:
        session: A decrypted profile from ``db.fetch_session``.
        timeout: Connect, banner and auth timeout in seconds.
        on_learn_host_key: Called with the serialized key the first time a host
            is seen, so the caller can persist the pin.
        attempts: Total tries, including the first.

    Raises:
        HostKeyChanged: the pinned key no longer matches. Never retried.
        paramiko.AuthenticationException: bad credentials. Never retried.
        SSHUnavailable: transient failures that did not clear.
    """
    last: Optional[BaseException] = None

    for attempt in range(1, max(1, attempts) + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(
            PinnedHostKeyPolicy(session.get("host_key") or "", on_learn_host_key)
        )
        try:
            client.connect(**build_connect_kwargs(session, timeout=timeout))
        except BaseException as error:
            try:
                client.close()
            except Exception:
                pass
            if not _is_transient(error) or attempt >= attempts:
                if _is_transient(error):
                    raise SSHUnavailable(
                        f"{session.get('host', 'host')} refused the connection "
                        f"after {attempt} attempts. It may be rate-limiting or "
                        f"at its connection limit — try again shortly."
                    ) from error
                raise
            last = error
            logger.info(
                "transient SSH failure for %s (attempt %d/%d): %s",
                session.get("host"), attempt, attempts, error,
            )
            # Back off so a retry does not itself add to the pressure.
            time.sleep(0.6 * attempt)
            continue

        transport = client.get_transport()
        if transport is not None:
            # 30s keepalive stops an idle NAT or firewall dropping the session.
            transport.set_keepalive(30)
        if attempt > 1:
            logger.info("connected to %s on attempt %d", session.get("host"), attempt)
        return client

    raise SSHUnavailable(str(last))
