"""Who is calling, and what they may do (Step 40, docs/PRODUCTION_PLAN.md).

Until this module existed the API had no authentication, and its own CORS
comment said so: *"this API has no auth, and a wildcard would let any website a
user visits call their local engine."* That was true and fine for one reviewer
on one laptop. It stops being fine the moment a second person can reach the
port, because everything the rest of the hardening keys on - the rate limit,
the spend ledger, and soon the owner of a chat session - keys on a *caller*,
and "the client address" is not a caller when there is a proxy in front.

Three modes, one seam
---------------------
`AUDITREADY_AUTH` picks an authenticator; every route reaches it through the
same dependency, so a route never knows which one is installed.

* `none` (default) - the actor is the client address, everyone is an auditor.
  This is today's behaviour, kept as the default on purpose: `uv run pytest`,
  the demo and a developer laptop must not need a credential to work.
* `token` - bearer tokens from `AUDITREADY_API_TOKENS`
  (`name:token` or `name:token:role1|role2`, comma-separated). Enough for a
  team sharing one deployment, for CI, and for an MCP client. Tokens are held
  as SHA-256 digests and compared in constant time, so a timing difference
  does not leak a prefix and a heap dump does not hand over a working key.
* `oidc` - the customer's identity provider. The bearer token is a JWT
  validated against the issuer's JWKS (`AUDITREADY_OIDC_ISSUER`,
  `AUDITREADY_OIDC_AUDIENCE`). This is the mode a compliance department
  actually wants: their directory, their MFA policy, their offboarding. We
  verify; we never issue.

Why not a hosted identity vendor. The product's one hard claim is that the
customer's evidence stays inside their network. Adding a service that every
sign-in must reach contradicts it, and puts a vendor between an auditor and
their own records. `oidc` delegates the hard parts (passwords, MFA, recovery,
offboarding) to the directory the customer already runs.

Why roles are two words. `auditor` may spend money - run an audit, hold a
conversation. `viewer` may read what exists. Anything finer is a policy
engine, and a policy engine before a second real deployment is speculation.

Req: CR-8; carries FR-15 (a refusal is a sentence, never a stack trace).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from core.tracing import log_event

log = logging.getLogger("auditready.auth")

ROLE_AUDITOR = "auditor"  # may spend money: run an audit, hold a conversation
ROLE_VIEWER = "viewer"    # may read packs, records and finished reports
ALL_ROLES = frozenset({ROLE_AUDITOR, ROLE_VIEWER})

# The challenge a 401 carries. A client that gets this knows what to send next.
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="auditready"'}


@dataclass(frozen=True)
class Actor:
    """The caller. `id` is what the rate limiter, the ledger and run ownership key on."""

    id: str
    name: str
    roles: frozenset[str] = ALL_ROLES
    auth: str = "none"  # which mode vouched for this actor

    def may(self, role: str) -> bool:
        return role in self.roles


class Authenticator(Protocol):
    mode: str

    def authenticate(self, request: Request) -> Actor: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def bearer_token(request: Request) -> str | None:
    """The token from `Authorization: Bearer <t>`, or None. Scheme is case-insensitive."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers=_CHALLENGE)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "anonymous"


def parse_roles(raw: str | None, default: frozenset[str] = ALL_ROLES) -> frozenset[str]:
    """`auditor|viewer` (or a list) to a role set. Unknown names are dropped, not trusted."""
    if raw is None:
        return default
    names = [n.strip().lower() for n in raw.replace(",", "|").split("|") if n.strip()]
    roles = frozenset(n for n in names if n in ALL_ROLES)
    return roles or frozenset()


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------


class NoAuth:
    """No credential asked for. The actor is the client address (today's behaviour).

    Kept as the default so the test suite, the demo and a laptop need no
    configuration. `GET /health` reports the mode, so "is this deployment
    open?" is a question anyone can answer without reading the env.
    """

    mode = "none"

    def authenticate(self, request: Request) -> Actor:
        host = _client_host(request)
        return Actor(id=host, name=host, roles=ALL_ROLES, auth="none")


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TokenRecord:
    digest: bytes
    name: str
    roles: frozenset[str]


class TokenAuth:
    """Bearer tokens from configuration, held as digests and compared in constant time."""

    mode = "token"

    def __init__(self, spec: str) -> None:
        self._records = self._parse(spec)
        if not self._records:
            raise ValueError(
                "AUDITREADY_AUTH=token but AUDITREADY_API_TOKENS is empty - set "
                "name:token[:roles] entries, comma-separated, or use AUDITREADY_AUTH=none"
            )

    @staticmethod
    def _parse(spec: str) -> list[_TokenRecord]:
        records: list[_TokenRecord] = []
        for entry in spec.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                raise ValueError(
                    f"AUDITREADY_API_TOKENS entry {entry.split(':')[0]!r} is malformed - "
                    "expected name:token or name:token:roles"
                )
            name, token = parts[0].strip(), parts[1].strip()
            roles = parse_roles(parts[2] if len(parts) > 2 else None)
            if not roles:
                raise ValueError(f"AUDITREADY_API_TOKENS entry {name!r} lists no known role")
            records.append(_TokenRecord(digest=_digest(token), name=name, roles=roles))
        return records

    def authenticate(self, request: Request) -> Actor:
        token = bearer_token(request)
        if token is None:
            raise _unauthorized("This deployment needs a token. Send Authorization: Bearer <token>.")
        offered = _digest(token)
        # Every record is compared, and always all of them: returning early on
        # the first match would make the answer's timing depend on which token
        # was offered.
        found: _TokenRecord | None = None
        for record in self._records:
            if secrets.compare_digest(offered, record.digest):
                found = record
        if found is None:
            log_event(log, "auth.rejected", logging.WARNING, mode=self.mode,
                      client=_client_host(request))
            raise _unauthorized("That token is not valid.")
        return Actor(id=f"token:{found.name}", name=found.name, roles=found.roles, auth="token")


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# oidc
# ---------------------------------------------------------------------------


@dataclass
class OidcSettings:
    issuer: str
    audience: str
    jwks_uri: str | None = None
    roles_claim: str = "roles"
    default_roles: frozenset[str] = ALL_ROLES
    algorithms: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384")
    discovery_timeout_s: float = 10.0


class OidcAuth:
    """Validate a JWT the customer's identity provider issued. We verify; we never issue.

    The signature is checked against the issuer's published keys, and so are
    `iss`, `aud` and `exp` - all three, because a token that is merely
    *well-formed* proves nothing, and a token minted for a different audience
    is someone else's credential being replayed at us.

    The key set is discovered once from `<issuer>/.well-known/openid-configuration`
    and cached by PyJWT's JWKS client, which refetches when it meets a `kid` it
    does not know - that is what makes an ordinary key rotation a non-event.
    """

    mode = "oidc"

    def __init__(self, settings: OidcSettings, jwk_client: Any | None = None) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._client = jwk_client
        self._discovered_at = 0.0

    # -- key discovery ---------------------------------------------------------

    def _jwks_uri(self) -> str:
        if self.settings.jwks_uri:
            return self.settings.jwks_uri
        url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            response = httpx.get(url, timeout=self.settings.discovery_timeout_s)
            response.raise_for_status()
            uri = response.json()["jwks_uri"]
        except Exception as exc:  # noqa: BLE001 - surfaced as a 503, never as a trace
            raise HTTPException(
                status_code=503,
                detail="The identity provider could not be reached to check your sign-in.",
            ) from exc
        self.settings.jwks_uri = uri
        return uri

    def _keys(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = PyJWKClient(self._jwks_uri(), cache_keys=True)
                self._discovered_at = time.time()
            return self._client

    # -- the check -------------------------------------------------------------

    def authenticate(self, request: Request) -> Actor:
        token = bearer_token(request)
        if token is None:
            raise _unauthorized("Sign in first: send the identity provider's token as Authorization: Bearer <jwt>.")
        try:
            key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.settings.algorithms),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except HTTPException:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise _unauthorized("Your sign-in has expired. Sign in again.") from exc
        except jwt.InvalidTokenError as exc:
            # The reason is for the log, not for the caller: telling an attacker
            # which check failed is telling them what to fix.
            log_event(log, "auth.rejected", logging.WARNING, mode=self.mode,
                      client=_client_host(request), reason=type(exc).__name__)
            raise _unauthorized("That sign-in is not valid here.") from exc

        subject = str(claims.get("sub"))
        name = str(claims.get("preferred_username") or claims.get("email") or subject)
        roles = self._roles(claims)
        if not roles:
            raise HTTPException(
                status_code=403,
                detail="Your account has no AuditReady role. Ask an administrator for access.",
            )
        return Actor(id=f"oidc:{subject}", name=name, roles=roles, auth="oidc")

    def _roles(self, claims: dict[str, Any]) -> frozenset[str]:
        raw = claims.get(self.settings.roles_claim)
        if raw is None:
            # The directory decided this person may sign in; without a role
            # claim we grant the configured default rather than guessing at a
            # hierarchy the customer has not described.
            return self.settings.default_roles
        if isinstance(raw, str):
            return parse_roles(raw, default=frozenset())
        if isinstance(raw, (list, tuple)):
            return parse_roles("|".join(str(r) for r in raw), default=frozenset())
        return frozenset()


# ---------------------------------------------------------------------------
# Building one from settings
# ---------------------------------------------------------------------------


def build_authenticator(mode: str, *, tokens: str = "", oidc: OidcSettings | None = None) -> Authenticator:
    """The one place a mode name becomes an authenticator. Unknown modes fail at startup."""
    mode = (mode or "none").strip().lower()
    if mode == "none":
        return NoAuth()
    if mode == "token":
        return TokenAuth(tokens)
    if mode == "oidc":
        if oidc is None or not oidc.issuer or not oidc.audience:
            raise ValueError(
                "AUDITREADY_AUTH=oidc needs AUDITREADY_OIDC_ISSUER and AUDITREADY_OIDC_AUDIENCE"
            )
        return OidcAuth(oidc)
    raise ValueError(f"AUDITREADY_AUTH must be none, token or oidc; got {mode!r}")
