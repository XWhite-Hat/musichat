"""
DPoP (Demonstration of Proof-of-Possession) — RFC 9449.

Module-level state: one EC P-256 keypair per process, loaded once at startup
by calling load_or_generate().  All DPoP proof creation uses this keypair.

Channel A — Streamer PC → Cloudflare Worker
  The streamer's JWK is registered with the Worker at /exchange time.
  Every subsequent call to Worker protected endpoints includes a DPoP proof
  in the 'DPoP' request header.

Channel B — Mod browser → Streamer PC
  Each mod session generates an ephemeral keypair in the browser.
  The public JWK is registered with the streamer server at /auth/register-dpop time.
  verify_proof() validates incoming mod DPoP proofs on the server side.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Optional

# ── Module-level keypair (Streamer PC keypair, loaded once at startup) ────────

_private_key = None           # EC P-256 private key
_public_jwk: Optional[dict] = None  # public key as JWK dict

# ── JTI replay prevention (in-memory, resets on process restart) ──────────────

_JTI_STORE: dict[str, float] = {}
_JTI_MAX_AGE = 120  # seconds — keep JTIs for 2 minutes to cover the ±60s iat window


# ── Public API ─────────────────────────────────────────────────────────────────

def load_or_generate() -> dict:
    """
    Load the streamer's DPoP keypair from the OS credential store, or generate
    a new one and persist it.  Returns the public JWK dict.

    Must be called once at app startup before any DPoP proof creation.
    """
    global _private_key, _public_jwk

    import secure_store as _ss
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    pem = _ss.get(_ss.DPOP_PRIVATE_KEY)

    if pem:
        try:
            _private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception:
            pem = ""  # corrupted — regenerate

    if not pem:
        _private_key = ec.generate_private_key(ec.SECP256R1())
        pem = _private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        _ss.put(_ss.DPOP_PRIVATE_KEY, pem)

    _public_jwk = _compute_public_jwk(_private_key)
    return dict(_public_jwk)


def get_public_jwk() -> Optional[dict]:
    """Return the current public JWK, or None if not yet initialized."""
    return dict(_public_jwk) if _public_jwk else None


def make_proof(method: str, url: str, access_token: str = "") -> str:
    """
    Create a DPoP proof JWT (typ: dpop+jwt, alg: ES256) for the given HTTP request.

    Parameters
    ----------
    method       HTTP method (GET, POST, …)
    url          Full request URL; query string is stripped per RFC 9449 §4.2
    access_token If provided, adds an 'ath' claim (SHA-256 of the token)

    Raises RuntimeError if the keypair hasn't been loaded yet.
    """
    if _private_key is None or _public_jwk is None:
        raise RuntimeError(
            "dpop_utils: keypair not loaded — call load_or_generate() at startup"
        )

    payload: dict = {
        "jti": str(uuid.uuid4()),
        "htm": method.upper(),
        "htu": url.split("?")[0],
        "iat": int(time.time()),
    }
    if access_token:
        payload["ath"] = _b64url_encode(
            hashlib.sha256(access_token.encode()).digest()
        )

    import jwt as _jwt
    return _jwt.encode(
        payload,
        _private_key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "jwk": _public_jwk},
    )


def dpop_header(method: str, url: str, access_token: str = "") -> dict[str, str]:
    """
    Return {"DPoP": "<proof>"}, or {} if the keypair isn't loaded yet.

    Safe to call even before load_or_generate() (BYOI mode callers don't need
    DPoP but call the same auth helpers).
    """
    try:
        return {"DPoP": make_proof(method, url, access_token)}
    except RuntimeError:
        return {}


def verify_proof(
    proof: str,
    method: str,
    url: str,
    stored_jwk: dict,
    access_token: str = "",
) -> bool:
    """
    Verify a DPoP proof JWT received from a mod browser (Channel B).

    Parameters
    ----------
    proof        The raw 'DPoP' header value
    method       HTTP method of the request the proof accompanies
    url          Full request URL (query string stripped internally)
    stored_jwk   The JWK the mod registered at /auth/register-dpop time
    access_token If provided, the 'ath' claim is verified against it
    """
    try:
        parts = proof.split(".")
        if len(parts) != 3:
            return False

        header  = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))

        if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
            return False

        proof_jwk = header.get("jwk", {})
        if not proof_jwk:
            return False

        # Key binding: proof must use the registered keypair
        if jwk_thumbprint(proof_jwk) != jwk_thumbprint(stored_jwk):
            return False

        # Signature verification: convert JWA P1363 (raw r||s) → DER for cryptography
        pub_key = _import_ec_pubkey(proof_jwk)
        sig_bytes = _b64url_decode(parts[2])
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        der_sig = encode_dss_signature(r, s)

        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives import hashes as _hashes
        pub_key.verify(
            der_sig,
            f"{parts[0]}.{parts[1]}".encode(),
            _ec.ECDSA(_hashes.SHA256()),
        )

        # Timestamp check (±60s)
        now = int(time.time())
        if abs(now - payload.get("iat", 0)) > 60:
            return False

        # Method and URL checks
        if payload.get("htm", "").upper() != method.upper():
            return False
        if payload.get("htu", "").split("?")[0] != url.split("?")[0]:
            return False

        # Replay prevention
        jti = payload.get("jti", "")
        if not jti or _is_jti_replayed(jti):
            return False
        _record_jti(jti)

        # Access-token binding
        if access_token:
            expected = _b64url_encode(hashlib.sha256(access_token.encode()).digest())
            if payload.get("ath") != expected:
                return False

        return True
    except Exception:
        return False


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint: SHA-256(canonical JSON), base64url-encoded."""
    # Required members in lexicographic order (crv, kty, x, y for EC P-256)
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url_encode(hashlib.sha256(canonical.encode()).digest())


# ── Internal helpers ───────────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _compute_public_jwk(private_key) -> dict:
    pub = private_key.public_key()
    nums = pub.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x":   _b64url_encode(nums.x.to_bytes(32, "big")),
        "y":   _b64url_encode(nums.y.to_bytes(32, "big")),
    }


def _import_ec_pubkey(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    return _ec.EllipticCurvePublicNumbers(x, y, _ec.SECP256R1()).public_key()


def _is_jti_replayed(jti: str) -> bool:
    _prune_jtis()
    return jti in _JTI_STORE


def _record_jti(jti: str) -> None:
    _prune_jtis()
    _JTI_STORE[jti] = time.monotonic() + _JTI_MAX_AGE


def _prune_jtis() -> None:
    now = time.monotonic()
    for k in [k for k, exp in list(_JTI_STORE.items()) if now > exp]:
        del _JTI_STORE[k]
