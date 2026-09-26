"""
Entra ID (Azure AD) bearer-token validation and the AUTH_MODE switch (#10).

Three modes, chosen so existing no-auth/shared-token deployments (and every
test written before this landed) keep working with zero config changes:

- ``none``  - no auth at all (today's local-demo default when
  STORYBIBLE_TOKEN is unset). Every caller is the same synthetic LOCAL_USER.
- ``token`` - today's shared-secret X-Token check (default when
  STORYBIBLE_TOKEN is set). Every caller is the same synthetic SHARED_USER.
- ``entra`` - validate a real `Authorization: Bearer` JWT against Entra's
  JWKS and return the real signed-in person (or the review-pipeline daemon,
  via its app role rather than a person's delegated scope).

Only ``entra`` mode has real per-user identity, so it's the only mode
app/main.py's ownership/sharing checks (#11) actually enforce - the other
two keep today's single-shared-bible behaviour.

This module deliberately does no database access (upserting the `users`
table and claiming ownerless series on first sign-in is DB work, so it
stays in main.py's get_current_user - see there) - it only decodes and
validates a token into a CurrentUser. That keeps it trivially unit
testable: sign a JWT with a local RSA key and monkeypatch _jwks_client
(see tests/test_auth.py) rather than reaching Entra's real JWKS endpoint.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

logger = logging.getLogger("storybible")

TOKEN = os.environ.get("STORYBIBLE_TOKEN", "").strip()
AUTH_MODE = os.environ.get("AUTH_MODE", "token" if TOKEN else "none").strip().lower()
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "").strip()
ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID", "").strip()
ALLOWED_OIDS = {x.strip() for x in os.environ.get("ALLOWED_OIDS", "").split(",") if x.strip()}

if AUTH_MODE not in ("none", "token", "entra"):
    raise RuntimeError(f"AUTH_MODE must be 'none', 'token' or 'entra', got {AUTH_MODE!r}")
if AUTH_MODE == "entra" and not (ENTRA_TENANT_ID and ENTRA_CLIENT_ID):
    raise RuntimeError("AUTH_MODE=entra requires ENTRA_TENANT_ID and ENTRA_CLIENT_ID")

PIPELINE_ROLE = "Pipeline.Read"
USER_SCOPE = "access_as_user"


@dataclass(frozen=True)
class CurrentUser:
    oid: str
    email: str
    display_name: str
    is_pipeline: bool = False


# Sentinel identities for the two auth-free modes - stable so created_by/
# updated_by and any joins against them behave consistently, but never
# valid Entra object ids (those are GUIDs), so they can't collide with a
# real person once a deployment switches to entra mode.
LOCAL_USER = CurrentUser(oid="local", email="", display_name="Local", is_pipeline=False)
SHARED_USER = CurrentUser(oid="shared", email="", display_name="Shared token", is_pipeline=False)
# For in-process callers (the nightly backup job - see app/backup.py) that
# call a route function directly rather than through a real request, and
# need to see every series regardless of AUTH_MODE/ownership. is_pipeline
# reuses main.py's existing "read anything, write nothing" bypass rather
# than adding a second special case for the same behaviour.
SYSTEM_USER = CurrentUser(oid="system", email="", display_name="Story Bible (system)", is_pipeline=True)


def jwks_url() -> str:
    return f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/discovery/v2.0/keys"


def issuer() -> str:
    return f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"


_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Lazy + cached, so importing this module (e.g. for AUTH_MODE=none
    tests) never makes a network call, and the real client is only built
    once per process. Tests substitute their own object here (anything
    with a `.get_signing_key_from_jwt(token).key` method, matching
    PyJWKClient's own interface) rather than reaching the real endpoint."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(jwks_url(), cache_keys=True)
    return _jwks_client


class AuthError(Exception):
    """Carries an HTTP status (401 or 403) without importing FastAPI here -
    keeps this module's only real dependency PyJWT, for easy unit testing."""
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def resolve_entra_user(token: str) -> CurrentUser:
    """Validate `token` against Entra's JWKS and turn it into a CurrentUser.
    Raises AuthError(401) for anything wrong with the token itself, and
    AuthError(403) for a token that's valid but lacks the required scope/
    role, or (for a real person) isn't on ALLOWED_OIDS. Never logs the
    token itself, only the failure reason."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=ENTRA_CLIENT_ID,
            issuer=issuer(),
            leeway=60,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as e:
        logger.warning("entra auth failed: %s", e)
        raise AuthError(401, "Invalid or expired token") from e

    scopes = (claims.get("scp") or "").split()
    roles = claims.get("roles") or []
    is_person = USER_SCOPE in scopes
    is_pipeline = not is_person and PIPELINE_ROLE in roles
    if not is_person and not is_pipeline:
        logger.warning("entra auth failed: token has neither %s scope nor %s role", USER_SCOPE, PIPELINE_ROLE)
        raise AuthError(403, "Token missing required scope or role")

    if is_pipeline:
        return CurrentUser(oid=claims.get("oid", ""), email="", display_name="Review pipeline", is_pipeline=True)

    oid = claims.get("oid", "")
    if ALLOWED_OIDS and oid not in ALLOWED_OIDS:
        logger.warning("entra auth failed: oid not on ALLOWED_OIDS")
        raise AuthError(403, "Not authorized")
    display_name = claims.get("name") or claims.get("preferred_username") or oid
    email = claims.get("preferred_username") or claims.get("email") or ""
    return CurrentUser(oid=oid, email=email, display_name=display_name, is_pipeline=False)


def bearer_token(authorization: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.
    Raises AuthError(401) if it's missing or malformed."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "Missing bearer token")
    token = authorization[len("bearer "):].strip()
    if not token:
        raise AuthError(401, "Missing bearer token")
    return token
