"""Who may call, and what they may do (Step 40). Offline — the OIDC key is made here.

What is pinned: `none` keeps the old behaviour; `token` refuses a missing or
wrong credential with a challenge and admits a right one; a role a token does
not hold is a 403, not a 500; an OIDC token is accepted only when its
signature, issuer, audience and expiry all hold; `/health` answers without a
credential in every mode and says which one is in force; and the rate limiter
and the ledger key on the authenticated actor rather than on the address.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main as api
from api.auth import (
    ALL_ROLES, Actor, NoAuth, OidcAuth, OidcSettings, TokenAuth, bearer_token,
    build_authenticator, parse_roles,
)
from api.ratelimit import RateLimits
from api.settings import Settings

ISSUER = "https://id.example.test/realms/meridian"
AUDIENCE = "auditready"


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


def fake_request(headers: dict[str, str] | None = None, host: str = "10.0.0.7"):
    """The three things the authenticators read off a request."""
    return SimpleNamespace(
        headers={k.lower(): v for k, v in (headers or {}).items()},
        client=SimpleNamespace(host=host),
    )


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def oidc(rsa_key):
    """An OIDC authenticator whose key set is the test's own key — no network."""
    client = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=rsa_key.public_key())
    )
    return OidcAuth(
        OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_uri="https://unused.test/jwks"),
        jwk_client=client,
    )


def make_jwt(rsa_key, **overrides) -> str:
    claims = {
        "sub": "u-1234",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "preferred_username": "dana.auditor",
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256")


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------


def test_bearer_token_is_read_case_insensitively_and_nothing_else_is():
    assert bearer_token(fake_request({"Authorization": "Bearer abc"})) == "abc"
    assert bearer_token(fake_request({"authorization": "bearer abc"})) == "abc"
    assert bearer_token(fake_request({"Authorization": "Basic abc"})) is None
    assert bearer_token(fake_request({"Authorization": "Bearer   "})) is None
    assert bearer_token(fake_request()) is None


def test_unknown_role_names_are_dropped_not_trusted():
    assert parse_roles("auditor|viewer") == ALL_ROLES
    assert parse_roles("viewer") == frozenset({"viewer"})
    assert parse_roles("auditor,viewer") == ALL_ROLES
    assert parse_roles("admin") == frozenset()          # not a role we know
    assert parse_roles(None) == ALL_ROLES               # absent = the default
    assert parse_roles("", default=frozenset()) == frozenset()


def test_the_mode_name_decides_the_authenticator_and_a_typo_fails_at_startup():
    assert build_authenticator("none").mode == "none"
    assert build_authenticator("token", tokens="ci:secret").mode == "token"
    with pytest.raises(ValueError, match="none, token or oidc"):
        build_authenticator("oauth")
    with pytest.raises(ValueError, match="AUDITREADY_API_TOKENS is empty"):
        build_authenticator("token", tokens="  ")
    with pytest.raises(ValueError, match="ISSUER"):
        build_authenticator("oidc", oidc=OidcSettings(issuer="", audience=""))


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------


def test_none_mode_keeps_the_old_behaviour():
    actor = NoAuth().authenticate(fake_request())
    assert actor.id == "10.0.0.7" and actor.auth == "none"
    assert actor.may("auditor") and actor.may("viewer")


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------


def test_a_token_is_required_and_must_be_the_right_one():
    auth = TokenAuth("ci:s3cret,reviewer:hunter2:viewer")

    with pytest.raises(HTTPException) as exc:
        auth.authenticate(fake_request())
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"].startswith("Bearer")
    assert "Authorization: Bearer" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        auth.authenticate(fake_request({"Authorization": "Bearer s3cre"}))  # a prefix
    assert exc.value.status_code == 401

    actor = auth.authenticate(fake_request({"Authorization": "Bearer s3cret"}))
    assert actor == Actor(id="token:ci", name="ci", roles=ALL_ROLES, auth="token")


def test_a_token_carries_only_the_roles_it_was_given():
    auth = TokenAuth("reviewer:hunter2:viewer")
    actor = auth.authenticate(fake_request({"Authorization": "Bearer hunter2"}))
    assert actor.roles == frozenset({"viewer"})
    assert actor.may("viewer") and not actor.may("auditor")


def test_the_raw_token_is_never_kept_in_memory():
    auth = TokenAuth("ci:s3cret")
    held = [record.digest for record in auth._records]
    assert all(isinstance(d, bytes) and len(d) == 32 for d in held)
    assert not any(b"s3cret" in d for d in held)


def test_a_malformed_token_entry_names_itself():
    with pytest.raises(ValueError, match="badentry"):
        TokenAuth("badentry")
    with pytest.raises(ValueError, match="no known role"):
        TokenAuth("ci:secret:admin")


# ---------------------------------------------------------------------------
# oidc
# ---------------------------------------------------------------------------


def test_a_well_signed_token_from_the_right_issuer_is_admitted(oidc, rsa_key):
    actor = oidc.authenticate(fake_request({"Authorization": f"Bearer {make_jwt(rsa_key)}"}))
    assert actor.id == "oidc:u-1234" and actor.name == "dana.auditor"
    assert actor.auth == "oidc" and actor.roles == ALL_ROLES  # no claim -> the default


def test_someone_elses_audience_is_refused(oidc, rsa_key):
    token = make_jwt(rsa_key, aud="some-other-app")
    with pytest.raises(HTTPException) as exc:
        oidc.authenticate(fake_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401
    assert "not valid here" in exc.value.detail  # the reason goes to the log, not the caller


def test_a_different_issuer_is_refused(oidc, rsa_key):
    token = make_jwt(rsa_key, iss="https://id.attacker.test/")
    with pytest.raises(HTTPException) as exc:
        oidc.authenticate(fake_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401


def test_an_expired_sign_in_says_so(oidc, rsa_key):
    token = make_jwt(rsa_key, exp=datetime.now(timezone.utc) - timedelta(seconds=5))
    with pytest.raises(HTTPException) as exc:
        oidc.authenticate(fake_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401 and "expired" in exc.value.detail


def test_a_token_signed_by_another_key_is_refused(oidc):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = make_jwt(other)
    with pytest.raises(HTTPException) as exc:
        oidc.authenticate(fake_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401


def test_roles_come_from_the_claim_when_there_is_one(oidc, rsa_key):
    actor = oidc.authenticate(
        fake_request({"Authorization": f"Bearer {make_jwt(rsa_key, roles=['viewer'])}"}))
    assert actor.roles == frozenset({"viewer"})

    actor = oidc.authenticate(
        fake_request({"Authorization": f"Bearer {make_jwt(rsa_key, roles='auditor|viewer')}"}))
    assert actor.roles == ALL_ROLES


def test_an_account_with_no_recognised_role_is_a_403_not_a_401(oidc, rsa_key):
    token = make_jwt(rsa_key, roles=["sales"])
    with pytest.raises(HTTPException) as exc:
        oidc.authenticate(fake_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 403 and "no AuditReady role" in exc.value.detail


# ---------------------------------------------------------------------------
# Through the real routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def token_client(monkeypatch, tmp_path):
    """The real app with token auth on, and the audit/chat seams faked."""
    from tests.test_api import FakeAgent, fake_runner  # the same fakes the API tests use
    from api.runs import RunRegistry
    from api.sessions import MemorySessionStore
    from api.auth import TokenAuth
    from core.spend import SpendLedger

    monkeypatch.setattr(api.app.state, "settings", Settings(reports_dir=tmp_path))
    monkeypatch.setattr(api.app.state, "auth", TokenAuth("ci:s3cret,reviewer:look-only:viewer"))
    monkeypatch.setattr(api.app.state, "runs", RunRegistry(tmp_path))
    monkeypatch.setattr(api.app.state, "sessions", MemorySessionStore())
    monkeypatch.setattr(api.app.state, "runner", fake_runner)
    monkeypatch.setattr(api.app.state, "agent_factory", FakeAgent)
    monkeypatch.setattr(api.app.state, "pricing", lambda model: (0.15, 0.60))
    monkeypatch.setattr(api.app.state, "ledger", SpendLedger(tmp_path / "spend.jsonl"))
    monkeypatch.setattr(api.app.state, "limits", RateLimits.build(30, 6, 400))
    return TestClient(api.app)


def test_health_answers_without_a_credential_and_names_the_mode(token_client):
    body = token_client.get("/health").json()
    assert body["ok"] is True and body["auth"] == "token"


def test_every_other_route_needs_the_token(token_client):
    for path in ("/packs", "/records", "/records/TCK-1001", "/audit", "/spend", "/pricing"):
        r = token_client.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith("Bearer")
    assert token_client.post("/chat", json={"question": "hi"}).status_code == 401
    assert token_client.post("/audit", json={"pack_id": "iso9001-sample"}).status_code == 401


def test_the_right_token_gets_through_and_is_named_in_spend(token_client):
    auth = {"Authorization": "Bearer s3cret"}
    assert len(token_client.get("/packs", headers=auth).json()) >= 2
    assert token_client.get("/spend", headers=auth).json()["actor"] == "ci"


def test_a_viewer_may_read_but_not_spend(token_client):
    viewer = {"Authorization": "Bearer look-only"}
    assert token_client.get("/records", headers=viewer).status_code == 200

    r = token_client.post("/chat", json={"question": "hi"}, headers=viewer)
    assert r.status_code == 403 and "auditor role" in r.json()["detail"]
    r = token_client.post("/audit", json={"pack_id": "iso9001-sample"}, headers=viewer)
    assert r.status_code == 403


def test_limits_and_spend_key_on_the_account_not_the_address(token_client, monkeypatch):
    """Two accounts from one address get their own bucket and their own bill."""
    monkeypatch.setattr(api.app.state, "auth",
                        TokenAuth("alice:alice-key,bob:bob-key"))
    monkeypatch.setattr(api.app.state, "limits", RateLimits.build(1, 6, 400))
    alice = {"Authorization": "Bearer alice-key"}
    bob = {"Authorization": "Bearer bob-key"}

    assert token_client.post("/chat", json={"question": "one"}, headers=alice).status_code == 200
    assert token_client.post("/chat", json={"question": "two"}, headers=alice).status_code == 429
    assert token_client.post("/chat", json={"question": "one"}, headers=bob).status_code == 200

    assert token_client.get("/spend", headers=alice).json()["today_usd"] > 0
    assert token_client.get("/spend", headers=bob).json()["today_usd"] == pytest.approx(
        token_client.get("/spend", headers=alice).json()["today_usd"])
    assert token_client.get("/spend", headers=bob).json()["deployment_today_usd"] == pytest.approx(
        2 * token_client.get("/spend", headers=bob).json()["today_usd"])


def test_a_run_started_with_a_token_records_who_started_it(token_client):
    auth = {"Authorization": "Bearer s3cret"}
    run_id = token_client.post(
        "/audit",
        json={"pack_id": "iso9001-sample", "ticket_ids": ["TCK-1001"],
              "control_ids": ["ISO9001-8.6-release-signoff"]},
        headers=auth,
    ).json()["run_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        if token_client.get(f"/audit/{run_id}", headers=auth).json()["status"] == "done":
            break
        time.sleep(0.05)
    assert api.app.state.runs.get(run_id).actor == "token:ci"
