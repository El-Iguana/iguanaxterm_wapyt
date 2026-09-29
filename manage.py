#!/usr/bin/env python3
"""
IguanaXterm administration from the command line.

    python manage.py health                  # exit 0 when the app answers
    python manage.py probe host.docker.internal 22       # can the app reach it?
    python manage.py users                   # list accounts
    python manage.py reset-password admin    # prompts for the new password
    python manage.py reset-password alice --create   # …or makes an admin

In a container (compose):

    docker compose exec iguanaxterm python manage.py users
    docker compose exec -it iguanaxterm python manage.py reset-password admin

``--password-stdin`` reads the password from standard input instead of
prompting, for scripts. It works on an existing database only; it does not
start the web service.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "appcode"))


def _load_dotenv() -> None:
    """The same .env the service reads when run outside a container."""
    env_path = HERE / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def health() -> int:
    """
    GET /healthz on this machine. The Host header is the canonical origin's,
    because behind a reverse proxy the app answers only to its public name.
    """
    import urllib.request

    port = os.getenv("PORT", "8765")
    origin = os.getenv("GANXTERM_CANONICAL_ORIGIN", f"http://127.0.0.1:{port}")
    host = urlsplit(origin).hostname or "127.0.0.1"
    request = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": host})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except Exception as exc:  # noqa: BLE001 - the exit code is the answer
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1


def probe(host: str, port: int) -> int:
    """
    Can this container open a TCP connection to ``host:port``? The first thing
    to check when a session times out: it tells a server that is not listening
    where the container looks apart from a wrong password or a firewall.
    """
    import socket

    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as exc:
        print(f"{host}: name does not resolve here ({exc})")
        return 1
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        print(f"{host} ({', '.join(addresses)}) port {port}: NOT reachable — {exc}")
        if "refused" in str(exc).lower():
            print("  Something answers at that address but nothing listens on the port —\n"
                  "  often a server that listens on 127.0.0.1 only. See INSTALL.md,\n"
                  "  \"Servers on the same computer\".")
        return 1
    print(f"{host} ({', '.join(addresses)}) port {port}: reachable")
    return 0


def users() -> int:
    from services.db import get_db, init_db

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, is_admin, created_at FROM users ORDER BY username COLLATE NOCASE"
        ).fetchall()
    for row in rows:
        role = "admin" if row["is_admin"] else "user"
        print(f"{row['username']:<24} {role:<6} created {row['created_at']}")
    return 0


def reset_password(username: str, create: bool, from_stdin: bool) -> int:
    from services.db import get_db, hash_password, init_db

    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"New password for {username}: ")
        if getpass.getpass("Again: ") != password:
            print("The passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("A password needs at least 8 characters.", file=sys.stderr)
        return 1

    init_db()
    with get_db() as conn:
        cursor = conn.execute(
            # Whoever runs this chooses the password: no reminder to change it.
            "UPDATE users SET pw_hash = ?, must_change_pw = 0 WHERE username = ?",
            (hash_password(password), username),
        )
        if cursor.rowcount:
            print(f"Password changed for {username}.")
            return 0
        if not create:
            print(f"No user {username!r}; add --create to make an administrator.", file=sys.stderr)
            return 1
        conn.execute(
            "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, 1)",
            (username, hash_password(password)),
        )
    print(f"Created administrator {username}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="exit 0 when the app answers on this machine")
    probe_cmd = commands.add_parser("probe", help="check this container can reach host:port")
    probe_cmd.add_argument("host")
    probe_cmd.add_argument("port", type=int)
    commands.add_parser("users", help="list accounts")
    reset = commands.add_parser("reset-password", help="set a user's password")
    reset.add_argument("username")
    reset.add_argument("--create", action="store_true", help="create an administrator if missing")
    reset.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = parser.parse_args(argv)
    if args.command == "health":
        return health()
    if args.command == "probe":
        return probe(args.host, args.port)
    if args.command == "users":
        return users()
    return reset_password(args.username, args.create, args.password_stdin)


if __name__ == "__main__":
    sys.exit(main())
