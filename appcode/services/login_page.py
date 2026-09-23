"""
Relabel pytincture's login form from "Email" to "Username".

IguanaXterm accounts are usernames, not email addresses — as they were in the
original app — and ``users.username`` is what the authenticator looks up. pytincture
hardcodes ``<input type="email" ... required>`` on its login page
(``backend/app.py``), and a browser refuses to submit ``admin`` into a
``type="email"`` field, so without this the app is only reachable with an
email-shaped account name.

The dhxpyt attempt did the same byte replacement and would have **silently
no-opped** on any pytincture upgrade, leaving a login page nobody can use and no
clue why. The difference here is :func:`verify_login_markup`, called at startup:
if the expected markup is gone, it says so in the log while the service is
still booting.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("iguanaxterm.login")

# Exact fragments pytincture emits. Each must be found, or the rewrite is stale.
_REWRITES: tuple[tuple[bytes, bytes], ...] = (
    (b'type="email" name="email"', b'type="text" name="email"'),
    (b'placeholder="Email"', b'placeholder="Username"'),
    (b'value="Login with Email"', b'value="Sign in"'),
)


def rewrite(body: bytes) -> bytes:
    for old, new in _REWRITES:
        body = body.replace(old, new)
    return body


def verify_login_markup(login_html: bytes) -> list[str]:
    """Fragments this rewrite expects but could not find. Empty means healthy."""
    return [
        old.decode("utf-8", "replace") for old, _new in _REWRITES if old not in login_html
    ]


class LoginPageMiddleware:
    """
    Raw ASGI middleware that rewrites the login page body.

    Raw ASGI rather than ``BaseHTTPMiddleware`` because the latter cannot
    reliably rewrite a response body. Only the login path is buffered; every
    other request is passed straight through untouched.
    """

    def __init__(self, app, path_suffix: str = "/login") -> None:
        self._app = app
        self._suffix = path_suffix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").endswith(self._suffix):
            await self._app(scope, receive, send)
            return

        start_message: dict | None = None
        chunks: list[bytes] = []

        async def capture(message) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self._app(scope, receive, capture)

        if start_message is None:
            # The inner app produced no response (a redirect handled upstream,
            # or an error). Nothing to rewrite, and nothing to send.
            return

        headers = [
            (key, value)
            for key, value in start_message.get("headers", [])
            # Rewriting changes the length, and a stale Content-Length truncates
            # the page. Content-Encoding would mean the body is not plain HTML.
            if key.lower() not in (b"content-length", b"content-encoding")
        ]

        body = b"".join(chunks)
        is_html = any(
            key.lower() == b"content-type" and b"text/html" in value
            for key, value in headers
        )
        if is_html:
            body = rewrite(body)

        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start_message, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def check_at_startup() -> None:
    """
    Log loudly if pytincture's login markup no longer matches the rewrite.

    Reads pytincture's login handler source rather than fetching the page, so
    the check costs nothing and runs before the first request is served.
    """
    try:
        import inspect

        from pytincture.backend import app as backend

        login_source = inspect.getsource(backend.login).encode()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not verify login markup: %s", exc)
        return

    missing = verify_login_markup(login_source)
    if missing:
        logger.error(
            "Login page rewrite is stale — pytincture no longer emits %s. "
            "The sign-in form will ask for an email address and reject "
            "username logins. Update services/login_page.py.",
            ", ".join(repr(item) for item in missing),
        )
    else:
        logger.info("Login page rewrite verified against pytincture's markup.")
