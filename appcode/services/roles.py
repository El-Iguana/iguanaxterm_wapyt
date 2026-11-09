"""
The three account roles, lowest to highest. Server-side only.

- ``user``: connects to the sites in the lists assigned to them.
- ``power``: Power User. Saves their own sites, sets up Users.
- ``admin``: everything, including accounts of every role.

A role includes everything below it, so checks compare ranks
(:func:`at_least`) rather than names.
"""
from __future__ import annotations

from typing import Any, Mapping

USER = "user"
POWER = "power"
ADMIN = "admin"

# Lowest first: the index is the rank.
ROLES = (USER, POWER, ADMIN)

LABELS = {USER: "User", POWER: "Power User", ADMIN: "Admin"}


def is_role(value: Any) -> bool:
    return isinstance(value, str) and value in ROLES


def rank(role: Any) -> int:
    """The role's rank, or -1 for anything that isn't a role."""
    return ROLES.index(role) if is_role(role) else -1


def at_least(role: Any, minimum: str) -> bool:
    """True when ``role`` is ``minimum`` or higher. Unknown roles never pass."""
    if not is_role(minimum):
        raise ValueError(f"unknown role {minimum!r}")
    return rank(role) >= rank(minimum)


def roles_claim(role: str) -> list[str]:
    """
    The ``roles`` session claim for an account.

    pytincture's ``@bff_policy(roles=...)`` requires every listed role to be in
    this list, so each role carries the ones below it: ``roles={"power"}``
    then means "Power User or above".
    """
    return list(reversed(ROLES[: rank(role) + 1])) if is_role(role) else []


def role_of(user: Mapping[str, Any] | None) -> str:
    """
    The role in a session's claims.

    A session signed in before roles existed has no ``role`` claim, only
    ``is_admin``. It gets ``admin`` or the lowest role until it signs in again,
    so nothing gains rights it didn't have.
    """
    if not user:
        return ""
    role = user.get("role")
    if is_role(role):
        return role
    return ADMIN if user.get("is_admin") else USER
