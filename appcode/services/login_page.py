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

# The iguana, above the form. Styles are inline so this needs only the one
# anchor below — every extra anchor is another thing that can go stale.
# Served from the app's own /static mount, which satisfies `img-src 'self'`.
_LOGO = (
    b'<img src="/static/el_iguana_avatar.webp" alt="" width="88" height="88" '
    b'style="width:88px;height:88px;border-radius:50%;object-fit:cover;'
    b'display:block;margin:0 auto 14px;border:1px solid #334155;'
    b'box-shadow:0 6px 18px rgba(0,0,0,.45);">'
)

# Dark theme, to match the app the login page leads into. Appended as one
# override block rather than rewriting each rule in place: later rules of equal
# specificity win, so this needs a single anchor instead of a dozen, and a
# pytincture restyle can only cost us the theme rather than break the page.
_DARK_CSS = b"""<style>
  :root{color-scheme:dark;}
  body{background-color:#0f172a !important;color:#e2e8f0;}
  .login-container{
    background:#111827 !important;
    border:1px solid #1f2937;
    box-shadow:0 18px 48px rgba(0,0,0,.55) !important;
    min-width:320px;
  }
  .login-container h2{color:#f1f5f9;}
  .input-field{
    background:#0f172a;
    color:#e2e8f0;
    border:1px solid #334155 !important;
  }
  .input-field::placeholder{color:#64748b;}
  .input-field:focus{
    outline:none;
    border-color:#38bdf8 !important;
    box-shadow:0 0 0 3px rgba(56,189,248,.18);
  }
  .submit-button,.login-button{background-color:#2563eb !important;}
  .login-button:hover,.submit-button:hover{background-color:#1d4ed8 !important;}
  .divider{border-bottom-color:#334155 !important;}
  .divider span{background:#111827 !important;color:#94a3b8 !important;}
  .login-help-text{
    background:#0f172a !important;
    color:#93c5fd !important;
    border:1px solid #1e3a8a;
  }
</style>
</head>"""

# Exact fragments pytincture emits. Each must be found, or the rewrite is stale.
_REWRITES: tuple[tuple[bytes, bytes], ...] = (
    (b'type="email" name="email"', b'type="text" name="email"'),
    (b'placeholder="Email"', b'placeholder="Username"'),
    (b'value="Login with Email"', b'value="Sign in"'),
    # The generic "Welcome" becomes the app's own mark.
    (b"<h2>Welcome</h2>", _LOGO + b"<h2 style=\"margin:0 0 4px\">IguanaXterm</h2>"),
    # Appended last so it overrides the page's own stylesheet.
    (b"</head>", _DARK_CSS),
    (
        b"<p>Please log in to continue</p>",
        b'<p style="margin:0 0 18px;color:#64748b;font-size:13px;letter-spacing:.02em">'
        b"SSH &middot; Telnet &middot; SFTP</p>",
    ),
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
