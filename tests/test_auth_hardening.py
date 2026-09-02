"""
DPoP key registration and the /auth/token rate ceiling.

Two hardening changes that go together with turning DPOP_REQUIRED on in the
Worker:

  * The Worker now rejects a broadcaster with no registered DPoP key instead of
    quietly falling back to bearer-token auth.  That is only safe if the client
    cannot fail to register one, so the two ways it could — an uninitialised
    keypair, and a single failed sync — are both closed here.
  * /auth/token's per-IP limit is keyed on a header that a caller reaching the
    loopback port directly can rotate.  A header-independent ceiling means that
    path cannot escape limiting entirely.
"""
from __future__ import annotations

import time

import pytest


# ── DPoP keypair availability ─────────────────────────────────────────────────

def test_public_jwk_initialises_the_keypair_on_demand(monkeypatch, tmp_path):
    """
    get_public_jwk() used to return None when the keypair had not been loaded,
    and both callers that register the key with the Worker simply skipped —
    stranding that broadcaster with no key on file.
    """
    import dpop_utils

    monkeypatch.setattr(dpop_utils, "_public_jwk", None)
    monkeypatch.setattr(dpop_utils, "_private_key", None)

    stored: dict[str, str] = {}
    monkeypatch.setattr("secure_store.get", lambda k: stored.get(k, ""))
    monkeypatch.setattr("secure_store.put", lambda k, v: stored.__setitem__(k, v))

    jwk = dpop_utils.get_public_jwk()
    assert jwk is not None
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    assert jwk.get("x") and jwk.get("y")


def test_public_jwk_is_stable_across_calls(monkeypatch):
    """Re-initialising must not mint a new key each time."""
    import dpop_utils

    monkeypatch.setattr(dpop_utils, "_public_jwk", None)
    monkeypatch.setattr(dpop_utils, "_private_key", None)
    stored: dict[str, str] = {}
    monkeypatch.setattr("secure_store.get", lambda k: stored.get(k, ""))
    monkeypatch.setattr("secure_store.put", lambda k, v: stored.__setitem__(k, v))

    first = dpop_utils.get_public_jwk()
    second = dpop_utils.get_public_jwk()
    assert first == second


def test_public_jwk_returns_none_rather_than_raising(monkeypatch):
    """A broken credential store must not take the caller down with it."""
    import dpop_utils

    monkeypatch.setattr(dpop_utils, "_public_jwk", None)
    monkeypatch.setattr(dpop_utils, "_private_key", None)

    def _boom():
        raise RuntimeError("credential store unavailable")

    monkeypatch.setattr(dpop_utils, "load_or_generate", _boom)
    assert dpop_utils.get_public_jwk() is None


# ── Key sync retries ──────────────────────────────────────────────────────────

def _fake_jwk(monkeypatch):
    monkeypatch.setattr(
        "dpop_utils.get_public_jwk",
        lambda: {"kty": "EC", "crv": "P-256", "x": "aa", "y": "bb"},
    )


def test_sync_retries_a_transient_failure(monkeypatch):
    """
    Losing this call to a network blip used to leave the broadcaster with no
    key registered for the whole session.
    """
    import server.auth as auth
    _fake_jwk(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    attempts = {"n": 0}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    def _post(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("network blip")
        return _Resp()

    monkeypatch.setattr(auth.requests, "post", _post)
    assert auth.sync_dpop_key("tok", "123", "https://worker.invalid") is True
    assert attempts["n"] == 3


def test_sync_gives_up_after_three_attempts(monkeypatch):
    import server.auth as auth
    _fake_jwk(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    attempts = {"n": 0}

    def _post(*a, **k):
        attempts["n"] += 1
        raise ConnectionError("still down")

    monkeypatch.setattr(auth.requests, "post", _post)
    assert auth.sync_dpop_key("tok", "123", "https://worker.invalid") is False
    assert attempts["n"] == 3


def test_sync_does_not_retry_a_rejected_request(monkeypatch):
    """A bad token or malformed key will be rejected every time — retrying
    it only delays startup."""
    import server.auth as auth
    _fake_jwk(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    attempts = {"n": 0}

    class _Resp:
        status_code = 403

        def raise_for_status(self):
            raise AssertionError("should not be reached for a 4xx")

    def _post(*a, **k):
        attempts["n"] += 1
        return _Resp()

    monkeypatch.setattr(auth.requests, "post", _post)
    assert auth.sync_dpop_key("tok", "123", "https://worker.invalid") is False
    assert attempts["n"] == 1


def test_sync_does_retry_a_429(monkeypatch):
    """Rate limiting is transient, unlike the other 4xx codes."""
    import server.auth as auth
    _fake_jwk(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    attempts = {"n": 0}

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http error")

    def _post(*a, **k):
        attempts["n"] += 1
        return _Resp(429 if attempts["n"] == 1 else 200)

    monkeypatch.setattr(auth.requests, "post", _post)
    assert auth.sync_dpop_key("tok", "123", "https://worker.invalid") is True
    assert attempts["n"] == 2


# ── /auth/token rate ceiling ──────────────────────────────────────────────────

@pytest.fixture
def fresh_limiter(monkeypatch):
    import server.app as app
    monkeypatch.setattr(app, "_TOKEN_RL", {})
    app._TOKEN_RL_GLOBAL.clear()
    return app


def test_per_ip_limit_still_applies(fresh_limiter):
    app = fresh_limiter
    allowed = sum(1 for _ in range(30) if app._token_rate_ok("1.2.3.4"))
    assert allowed == app._TOKEN_RL_MAX


def test_separate_ips_get_separate_buckets(fresh_limiter):
    app = fresh_limiter
    assert app._token_rate_ok("1.1.1.1")
    assert app._token_rate_ok("2.2.2.2")


def test_rotating_the_ip_cannot_escape_limiting(fresh_limiter):
    """
    The forged-header scenario: a fresh IP per request would otherwise mean a
    fresh per-IP bucket every time and no effective limit at all.
    """
    app = fresh_limiter
    allowed = sum(1 for i in range(500) if app._token_rate_ok(f"10.0.{i // 256}.{i % 256}"))
    assert allowed == app._TOKEN_RL_GLOBAL_MAX
    assert allowed < 500


def test_the_global_ceiling_is_above_normal_use(fresh_limiter):
    """Token issuance happens at sign-in, not continuously."""
    app = fresh_limiter
    assert app._TOKEN_RL_GLOBAL_MAX > app._TOKEN_RL_MAX


def test_a_refused_request_does_not_consume_global_budget(fresh_limiter):
    """One spammer hitting their own per-IP wall must not exhaust everyone."""
    app = fresh_limiter
    for _ in range(100):
        app._token_rate_ok("9.9.9.9")          # blocked after 20
    assert len(app._TOKEN_RL_GLOBAL) == app._TOKEN_RL_MAX
    assert app._token_rate_ok("8.8.8.8") is True
