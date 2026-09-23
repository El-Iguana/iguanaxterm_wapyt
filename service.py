#!/usr/bin/env python3
"""
ASGI entrypoint for IguanaXterm.

    python service.py
    # or: uvicorn service:app --host 0.0.0.0 --port 8765

Everything server-side is wired here rather than inside the application module,
so ``appcode/iguanaxterm.py`` stays pure browser code. pytincture serves that
module's source to Pyodide; a server-only import sitting in it — even behind a
``sys.platform`` guard — is still visible to the AST pass that resolves the
widgetset and the entrypoint.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
APPCODE = HERE / "appcode"

# The BFF modules import each other as `services.<name>`, and pytincture loads
# them by path from the modules folder, so that folder has to be importable
# here too for the wiring below.
sys.path.insert(0, str(APPCODE))

def load_dotenv_file() -> None:
    """
    Load ``.env`` from the project root, if there is one.

    Compose reads it through `env_file`, but running `python service.py`
    directly did not — so the README's "copy .env.example to .env and edit"
    silently had no effect outside a container. Real environment variables
    always win, so an explicit `VAR=x python service.py` still overrides.
    """
    env_path = HERE / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv_file()

from pytincture import PytinctureConfig, create_app  # noqa: E402

from services.db import init_db, session_secret  # noqa: E402
from services.login_page import LoginPageMiddleware, check_at_startup  # noqa: E402
from services.sftp_service import sftp_pool  # noqa: E402
from services.terminal_ws import router as terminal_router  # noqa: E402
from services.transfer import router as transfer_router  # noqa: E402

APPLICATION = "iguanaxterm"
PORT = int(os.getenv("PORT", "8765"))


def canonical_origin() -> str:
    """
    The single origin this service is reached on.

    pytincture pins Origin and Fetch-Metadata checks to it, which is what makes
    a cross-site BFF call fail closed.
    """
    # The default is the literal IP, not "localhost": pytincture's loopback
    # check parses the host as an IP address, and a name never satisfies it.
    return os.getenv("GANXTERM_CANONICAL_ORIGIN", f"http://127.0.0.1:{PORT}").rstrip("/")


def _strip_port(value: str) -> str:
    """Drop a trailing :port, leaving IPv6 literals and bare names intact."""
    # A bare "[::1]" splits to tail="1]", which is not all digits, so the
    # bracketed form is safe without a special case.
    head, _, tail = value.rpartition(":")
    return head if head and tail.isdigit() else value


def allowed_hosts() -> tuple[str, ...]:
    """
    Exact Host values this service answers to.

    pytincture refuses to enable authentication without them — a wildcard Host
    is what makes DNS-rebinding attacks work. Behind a reverse proxy, set
    GANXTERM_ALLOWED_HOSTS to the public name.

    **Hostnames only, no ports.** Starlette's TrustedHostMiddleware compares
    against the Host header with the port stripped, and pytincture matches
    canonical_origin's bare hostname, so a "host:port" entry never matches
    anything. The canonical origin's hostname is always included, because
    pytincture rejects a configuration where it is missing — and the container
    case makes that easy to get wrong, since the published port differs from
    the one the app listens on inside.
    """
    configured = os.getenv("GANXTERM_ALLOWED_HOSTS", "").strip()
    if configured:
        hosts = [
            _strip_port(part.strip()) for part in configured.split(",") if part.strip()
        ]
    else:
        # IPv6 loopback is deliberately absent: pytincture's loopback check
        # wants the bracketed "[::1]" form while Starlette splits the Host
        # header on ":", and the two do not agree. Set GANXTERM_ALLOWED_HOSTS
        # if you genuinely need it.
        hosts = ["127.0.0.1"]

    canonical = urlsplit(canonical_origin()).hostname
    if canonical and canonical not in hosts:
        hosts.append(canonical)
    return tuple(dict.fromkeys(hosts))


def is_loopback_deployment(origin: str) -> bool:
    """True when this process is serving plain HTTP on localhost."""
    parsed = urlsplit(origin)
    if parsed.scheme == "https":
        return False
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


def build_app():
    init_db()

    # Both hooks are registered by dotted path rather than by calling
    # set_user_authenticator()/set_bff_policy_hook(). create_app() loads its own
    # isolated backend module, so a setter called against the shared
    # pytincture.backend.app never reaches it and startup fails with
    # "@bff_policy exports require BFF_POLICY_HOOK_PATH". The dotted form is
    # what the docs recommend for service mode, and it resolves per backend.
    #
    # AUTH_USER_AUTHENTICATOR is what points local login at our users table.
    # Without it pytincture falls back to the AUTH_PASSWORD_HASHES environment
    # variable and nobody can log in at all.
    hooks = {
        "AUTH_USER_AUTHENTICATOR": "services.auth.authenticate",
        "BFF_POLICY_HOOK_PATH": "services.auth.policy_hook",
        # Claims returned by the authenticator are dropped from the session
        # unless they are declared here: _build_auth_session_user() copies only
        # a fixed set plus whatever AUTH_SESSION_CLAIM_KEYS names. Without this
        # the BFF services see no user_id and every call is denied 403 — with
        # "BFF policy denied the operation" and nothing to say why.
        "AUTH_SESSION_CLAIM_KEYS": "user_id,is_admin,username",
    }

    origin = canonical_origin()
    # pytincture will not run authenticated over plain HTTP: production mode
    # demands an https canonical_origin and secure cookies. Loopback
    # development is the documented exception, and it is the only thing
    # enable_dev_email_login buys us here — pytincture's password-less loopback
    # branch is unreachable once set_user_authenticator() is installed, because
    # that path returns or raises before ever reaching it. Passwords are still
    # required in dev.
    loopback = is_loopback_deployment(origin)
    if loopback:
        print(
            "  Loopback development mode: plain HTTP on localhost.\n"
            "  Set GANXTERM_CANONICAL_ORIGIN to an https:// URL (and put TLS in\n"
            "  front) before exposing this to anything but your own machine.",
            flush=True,
        )

    application = create_app(
        PytinctureConfig(
            modules_path=str(APPCODE),
            default_application=APPLICATION,
            enable_user_login=True,
            enable_dev_email_login=loopback,
            session_secret=session_secret(),
            allowed_hosts=allowed_hosts(),
            canonical_origin=origin,
            trusted_proxy_headers=not loopback,
            environment=hooks,
        )
    )

    # Relabel pytincture's hardcoded email login field; see login_page.py.
    check_at_startup()
    application.add_middleware(LoginPageMiddleware)

    application.include_router(terminal_router)
    application.include_router(transfer_router)

    # xterm is served from this origin because pytincture's CSP is
    # `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:` and
    # `style-src 'self' 'unsafe-inline'` — a CDN is blocked outright. The
    # wapyt Terminal widget loads these four files from `assetBase`.
    from fastapi.staticfiles import StaticFiles

    application.mount(
        "/xterm", StaticFiles(directory=str(APPCODE / "vendor" / "xterm")), name="xterm"
    )

    @application.on_event("shutdown")
    async def _close_pool() -> None:
        sftp_pool.close_all()

    return application


app = build_app()


if __name__ == "__main__":
    import uvicorn

    print(
        f"""
  ___                               __  __
 |_ _|__ _ _  _  __ _ _ _  __ _   \\ \\/ /_ ______ _ __
  | |/ _` | || |/ _` | ' \\/ _` |   >  <  / -_) '_| '  \\
 |___\\__, |\\_,_|\\__,_|_||_\\__,_|  /_/\\_\\\\___|_| |_|_|_|
        |_|
  Browser-based SSH/Telnet Terminal Manager — pytincture/wapyt
  Developer : OldManGan <eliguana@protonmail.com>
  URL       : {canonical_origin()}/{APPLICATION}
""",
        flush=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
