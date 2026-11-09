"""
Authentication wiring.

Two hooks, with a deliberate split of responsibility:

``authenticate``
    Runs once, at login. Verifies the password against the ``users`` table and
    returns the claims that go into the session.

``policy_hook``
    Runs before every BFF call. Reads the claims that ``authenticate`` already
    established. It does **not** re-verify the password — the session is the
    credential once login has happened.

The dhxpyt rewrite got this backwards: it left pytincture's login pointed at
``AUTH_PASSWORD_HASHES`` (so nobody could log in at all) and then bcrypt-verified
a password on every single BFF call — a password that pytincture strips from the
session claims anyway, so every call 401'd. Login throttling is pytincture's
own (``AUTH_LOGIN_RATE_LIMIT_ATTEMPTS``, 20 per 60s per peer and per account).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.db import get_db, verify_password
from services.roles import ADMIN, at_least, is_role, role_of, roles_claim


def authenticate(email: str, password: str, request: Any = None) -> Optional[dict]:
    """
    Verify a login and return its session claims.

    pytincture casefolds the submitted name before calling this, which is why
    ``users.username`` is declared ``COLLATE NOCASE`` — otherwise an admin
    created as "Admin" could never log in.

    Returns:
        A claims mapping on success, or ``None`` to reject. Claim names must
        avoid pytincture's sensitive set (``password``, ``token``, ...), which
        is stripped before the session is written.
    """
    username = (email or "").strip()
    if not username or not password:
        return None

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, pw_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if row is None:
        # Spend the same time as a real verify so a wrong username and a wrong
        # password are not distinguishable by timing.
        verify_password(password, "$2b$12$" + "." * 53)
        return None

    if not verify_password(password, row["pw_hash"]):
        return None

    role = row["role"]
    return {
        "user_id": row["id"],
        "username": row["username"],
        "role": role,
        # Kept for one release, for anything not yet reading `role`.
        "is_admin": role == ADMIN,
        # pytincture enforces `roles` itself for @bff_policy(roles=...): every
        # listed role must be present, so each role carries the ones below it
        # and roles={"power"} means "Power User or above".
        "roles": roles_claim(role),
    }


def policy_hook(
    user: Mapping[str, Any],
    policy: Mapping[str, Any],
    class_name: str = "",
    function_name: str = "",
    **_context: Any,
) -> bool:
    """
    Authorise one BFF call.

    pytincture has already enforced every claim it recognises — ``roles``
    included — before this runs. What is left is the baseline check that the
    caller is someone, and the app's own keys:

    ``min_role``
        ``"user"``, ``"power"`` or ``"admin"``: the caller's role must be that
        or higher. An unknown value denies, so a typo fails closed.
    ``admin_required``
        The older spelling of ``min_role="admin"``, kept until every service
        has moved over.

    Contract: ``True``/``None`` allow, ``False`` denies, anything else fails
    closed with a RuntimeError.
    """
    if not user or not user.get("user_id"):
        return False
    role = role_of(user)
    minimum = policy.get("min_role")
    if minimum is not None and not (is_role(minimum) and at_least(role, minimum)):
        return False
    if policy.get("admin_required") and role != ADMIN:
        return False
    return True


def current_user_id(user: Optional[Mapping[str, Any]]) -> int:
    """
    The authenticated user's row id, or 0 when unauthenticated.

    BFF services scope every query by this. Returning 0 rather than raising
    keeps an unauthenticated call to a service that somehow got past the policy
    hook returning nothing instead of everything.
    """
    if not user:
        return 0
    try:
        return int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0
