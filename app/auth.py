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
_RAW_ALLOWED_OIDS = os.environ.get("ALLOWED_OIDS", "").strip()
# "*" is the explicit "any Entra user Assignment-required lets in" opt-out
# (#70): it leaves ALLOWED_OIDS empty, which resolve_entra_user treats as
# "don't add a check". A merely *blank* value is a startup error in entra
# mode (below) - otherwise forgetting to set it silently disables the
# allowlist rather than failing closed.
ALLOWED_OIDS = set() if _RAW_ALLOWED_OIDS == "*" else {x.strip() for x in _RAW_ALLOWED_OIDS.split(",") if x.strip()}
if "*" in ALLOWED_OIDS:
    # e.g. "*," or "abc,*": would otherwise be parsed as the literal oid "*",
    # start fine, and then 403 every real user.
    raise RuntimeError('ALLOWED_OIDS: "*" only works on its own (ALLOWED_OIDS=*), not inside a list')
# The one person allowed to claim series that predate auth (owner_oid == '',
# #11) - see main.py's get_current_user. Unset means nobody claims them.
LEGACY_OWNER_OID = os.environ.get("LEGACY_OWNER_OID", "").strip()
if any(ch in LEGACY_OWNER_OID for ch in ",* \t"):
    # A copy-paste of ALLOWED_OIDS's syntax: no oid would ever equal it, so
    # nothing would be claimed and the only symptom would be missing data.
    raise RuntimeError("LEGACY_OWNER_OID must be a single Entra object id")

# Who may use the admin routes (#82). Entra object ids, NEVER emails: with open
# signup (#85) the email / preferred_username claims are controlled by whichever
# tenant the person signs in from, so an email match would be forgeable. An oid
# is a GUID Entra assigns. Only honoured in AUTH_MODE=entra.
ADMIN_OIDS = {x.strip().lower() for x in os.environ.get("ADMIN_OIDS", "").split(",") if x.strip()}
if any(ch in oid for oid in ADMIN_OIDS for ch in "*@ \t"):
    raise RuntimeError("ADMIN_OIDS must be a comma-separated list of Entra object ids (not emails or '*')")

if AUTH_MODE not in ("none", "token", "entra"):
    raise RuntimeError(f"AUTH_MODE must be 'none', 'token' or 'entra', got {AUTH_MODE!r}")
if AUTH_MODE == "entra" and not (ENTRA_TENANT_ID and ENTRA_CLIENT_ID):
    raise RuntimeError("AUTH_MODE=entra requires ENTRA_TENANT_ID and ENTRA_CLIENT_ID")
if AUTH_MODE == "token" and not TOKEN:
    # Otherwise `if auth.TOKEN and x_token != auth.TOKEN` in main.py's
    # get_current_user is vacuously false for every request - AUTH_MODE=
    # token with a blank STORYBIBLE_TOKEN would silently accept everyone
    # while /api/health and /api/config both report auth as "on".
    raise RuntimeError("AUTH_MODE=token requires STORYBIBLE_TOKEN to be set")

if AUTH_MODE == "entra" and not ALLOWED_OIDS and _RAW_ALLOWED_OIDS != "*":
    raise RuntimeError(
        "AUTH_MODE=entra requires ALLOWED_OIDS (comma-separated Entra object ids), "
        "or ALLOWED_OIDS=* to deliberately allow everyone Entra itself lets in"
    )
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
        # timeout: PyJWKClient's default is 30 s, and it holds a lock while
        # fetching - so an unreachable Entra would otherwise stall every
        # request behind a 30 s wait. lifespan: keys are cached for an hour
        # (default 5 min) so a brief outage doesn't bite right after expiry;
        # an unknown kid (key rotation) still triggers a refresh, rate
        # limited by PyJWKClient's own cooldown.
        _jwks_client = PyJWKClient(jwks_url(), cache_keys=True, timeout=5, lifespan=3600)
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

    if not claims.get("oid"):
        # An empty oid would compare equal to the "ownerless" marker
        # (owner_oid == '') used for pre-auth data (#70) - real Entra tokens,
        # person or service principal, always carry one.
        logger.warning("entra auth failed: token has no oid claim")
        raise AuthError(401, "Invalid or expired token")

    scopes = (claims.get("scp") or "").split()
    roles = claims.get("roles") or []
    is_person = USER_SCOPE in scopes
    is_pipeline = not is_person and PIPELINE_ROLE in roles
    if not is_person and not is_pipeline:
        logger.warning("entra auth failed: token has neither %s scope nor %s role", USER_SCOPE, PIPELINE_ROLE)
        raise AuthError(403, "Token missing required scope or role")

    if is_pipeline:
        return CurrentUser(oid=claims["oid"], email="", display_name="Review pipeline", is_pipeline=True)

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
