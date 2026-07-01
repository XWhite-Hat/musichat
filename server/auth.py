"""
Auth helpers for the mod panel.

Flow
----
1. Mod visits /login  → redirected to Twitch OAuth (Implicit Grant, response_type=token)
2. Twitch redirects to /auth/callback with the access token in the URL *fragment*
3. A small JS page reads the fragment and POSTs the bare token to /auth/token
4. Server calls /helix/users with the token to get the username
5. Server calls /helix/moderation/moderators with the *streamer's* token
   to verify the visitor is actually a listed mod or the broadcaster
6. Issues a short-lived JWT for subsequent API calls

The JWT contains: {"sub": username, "exp": <unix ts>}

Note: the mod panel uses Implicit Grant (response_type=token) because it is a
browser-only flow with no server-side component — the token lands in the fragment
and is POSTed back.  Twitch does not support PKCE, so Auth Code flow here would
require embedding a client_secret in the distributed binary, which is not viable.
Streamer/bot sign-in (BYOI mode) uses Auth Code flow with a localhost redirect
and a user-supplied client_secret stored in DATA_DIR/.env.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

import jwt
import requests

_log = logging.getLogger(__name__)

TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_API = "https://api.twitch.tv/helix"

_OAUTH_STATE_STORE: dict[str, float] = {}  # state → created_at
STATE_TTL = 300  # 5 minutes

# ── Mod DPoP JWK registry (Channel B) ────────────────────────────────────────
# Maps username → public JWK registered at /auth/register-dpop time.
# In-memory only: cleared on server restart (mods re-authenticate naturally).

_MOD_DPOP_KEYS: dict[str, dict] = {}

# DPoP is optional for 60 s after startup so existing mod tabs can re-register
# their keypair after an app restart without hard-failing immediately.
_DPOP_GRACE_UNTIL: float = time.monotonic() + 60
_log.warning(
    "DPoP grace window active for 60 s — mod panel requests without a registered "
    "keypair will be accepted until %.0f (monotonic)",
    _DPOP_GRACE_UNTIL,
)


def dpop_grace_active() -> bool:
    """True while in the post-startup grace window — DPoP required once it expires."""
    return time.monotonic() < _DPOP_GRACE_UNTIL


def register_mod_dpop_jwk(username: str, jwk: dict) -> None:
    _MOD_DPOP_KEYS[username] = jwk


def get_mod_dpop_jwk(username: str) -> Optional[dict]:
    return _MOD_DPOP_KEYS.get(username)


_STATE_STORE_CAP = 500  # max concurrent in-flight OAuth flows

def generate_oauth_state() -> Optional[str]:
    now = time.time()
    # Prune expired states first so the cap isn't hit by abandoned flows.
    expired = [k for k, ts in _OAUTH_STATE_STORE.items() if now - ts >= STATE_TTL]
    for k in expired:
        del _OAUTH_STATE_STORE[k]
    if len(_OAUTH_STATE_STORE) >= _STATE_STORE_CAP:
        return None
    state = secrets.token_urlsafe(32)
    _OAUTH_STATE_STORE[state] = now
    return state


def validate_oauth_state(state: str) -> bool:
    ts = _OAUTH_STATE_STORE.pop(state, None)
    if ts is None:
        return False
    return (time.time() - ts) < STATE_TTL


def build_twitch_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """
    Build a Twitch Implicit Grant auth URL for the mod panel.

    Uses response_type=token (Implicit Grant) — no client_secret needed.
    The token arrives in the URL fragment; the callback page reads it via JS
    and POSTs it to /auth/token on the panel server.

    Required: register http://<host>:<port>/auth/callback as an additional
    redirect URI in your Twitch developer console alongside the main app URI.
    """
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "token",          # Implicit Grant — no client_secret
        "scope": "user:read:email user:read:moderated_channels",
        "state": state,
        # force_verify intentionally omitted — silent redirect for returning users
    }
    return f"{TWITCH_AUTH_URL}?{urlencode(params)}"


def get_twitch_user(
    access_token: str,
    client_id: str = "",
    broadcaster_id: str = "",
) -> Optional[dict]:
    """
    Look up the Twitch user who owns *access_token*.

    Proxied mode (client_id == ""): calls Worker /userinfo — the Worker adds
    Client-Id server-side.  broadcaster_id is required in proxied mode; the
    Worker uses it to verify the broadcaster has authenticated through this
    service before serving the request.
    BYOI mode (client_id set): calls Helix directly; broadcaster_id unused.
    """
    if not client_id:
        # Proxied: delegate to Worker /userinfo so client_id never lives here.
        # Worker returns the user dict directly (not wrapped in {data: []}).
        from constants import TWITCH_WORKER_URL
        import dpop_utils as _dpop
        try:
            _url = f"{TWITCH_WORKER_URL}/userinfo"
            resp = requests.post(
                _url,
                json={"access_token": access_token, "broadcaster_id": broadcaster_id},
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()  # worker already unwraps the first element
        except Exception as e:
            print(f"[auth] proxied user lookup failed: {e}")
            return None

    # BYOI: call Helix directly
    try:
        resp = requests.get(
            f"{TWITCH_API}/users",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Client-Id": client_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0] if data else None
    except Exception:
        return None


def is_mod_or_broadcaster(
    username: str,
    user_id: str,
    broadcaster_id: str,
    mod_token: str,
    client_id: str = "",
) -> bool:
    """
    Check whether username is the broadcaster or a listed mod.

    Uses the mod's OWN token (fresh at login time) rather than the streamer's
    stored token, so this never breaks due to stored-token expiry.

    Proxied mode (client_id == ""): calls Worker /is-mod — Worker adds
    Client-Id server-side and uses mod_token to call /helix/moderation/channels.
    BYOI mode (client_id set): calls Helix directly with mod_token.
    """
    if not client_id:
        from constants import TWITCH_WORKER_URL
        import dpop_utils as _dpop
        try:
            _url = f"{TWITCH_WORKER_URL}/is-mod"
            resp = requests.post(
                _url,
                json={
                    "username": username,
                    "broadcaster_id": broadcaster_id,
                    "mod_token": mod_token,
                },
                headers=_dpop.dpop_header("POST", _url, mod_token),
                timeout=10,
            )
            resp.raise_for_status()
            return bool(resp.json().get("is_mod", False))
        except Exception as e:
            print(f"[auth] proxied mod check failed: {e}")
            return False

    # BYOI: use the mod's own token — requires user:read:moderated_channels scope
    # (added to build_twitch_auth_url).  user_id is pre-resolved by the caller.
    try:
        if user_id == broadcaster_id:
            return True

        channels_resp = requests.get(
            f"{TWITCH_API}/moderation/channels",
            params={"user_id": user_id},
            headers={
                "Authorization": f"Bearer {mod_token}",
                "Client-Id": client_id,
            },
            timeout=10,
        )
        channels_resp.raise_for_status()
        broadcaster_ids = {
            ch["broadcaster_id"]
            for ch in channels_resp.json().get("data", [])
        }
        return broadcaster_id in broadcaster_ids
    except Exception as e:
        print(f"[auth] mod check failed: {e}")
        return False


def update_panel_origin(
    tunnel_url: str,
    access_token: str,
    broadcaster_id: str,
    worker_url: str,
) -> bool:
    """
    Tell the Worker the streamer's current external panel origin so mod logins
    are accepted from that URL.  Called automatically when the tunnel URL changes.
    No-ops silently on network failure — the old origin stays registered and mods
    on localhost still work; the next tunnel start will retry.
    """
    import dpop_utils as _dpop
    try:
        from urllib.parse import urlparse as _up
        origin = _up(tunnel_url).scheme + "://" + _up(tunnel_url).netloc
        _url = f"{worker_url}/update-panel-origin"
        resp = requests.post(
            _url,
            json={
                "access_token":   access_token,
                "broadcaster_id": broadcaster_id,
                "panel_origin":   origin,
            },
            headers=_dpop.dpop_header("POST", _url, access_token),
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[auth] update_panel_origin failed: {e}", file=__import__("sys").stderr)
        return False


def sync_dpop_key(
    access_token: str,
    broadcaster_id: str,
    worker_url: str,
) -> bool:
    """
    Push the current local DPoP public JWK to the Worker so it matches whatever
    keypair is in the OS credential store.  Called on startup to auto-recover
    from keypair drift (keyring cleared, new install, etc.).  No DPoP header is
    sent — the endpoint accepts token ownership alone as the auth mechanism.
    """
    import dpop_utils as _dpop
    jwk = _dpop.get_public_jwk()
    if not jwk:
        return False
    try:
        _url = f"{worker_url}/sync-dpop-key"
        resp = requests.post(
            _url,
            json={"access_token": access_token, "broadcaster_id": broadcaster_id, "dpop_jwk": jwk},
            timeout=10,
        )
        resp.raise_for_status()
        print("[auth] DPoP key synced with Worker", file=__import__("sys").stderr)
        return True
    except Exception as e:
        print(f"[auth] sync_dpop_key failed: {e}", file=__import__("sys").stderr)
        return False


def refresh_access_token(
    refresh_token: str,
    worker_url: str,
    broadcaster_id: str = "",
) -> Optional[dict]:
    """
    Exchange a refresh token for a new access token via the Worker /refresh
    endpoint.  Returns {access_token, refresh_token, expires_in} or None.

    Only used in proxied mode — BYOI tokens are refreshed directly against
    Twitch by TwitchBot._refresh_one_byoi() using the local client_secret.
    broadcaster_id is required by the Worker to verify the token belongs to a
    registered broadcaster; callers should always supply it.
    """
    import dpop_utils as _dpop
    try:
        _url = f"{worker_url}/refresh"
        resp = requests.post(
            _url,
            json={"refresh_token": refresh_token, "broadcaster_id": broadcaster_id},
            headers=_dpop.dpop_header("POST", _url),
            timeout=10,
        )
        if resp.status_code == 401:
            try:
                body = resp.json()
                if body.get("reauth_required"):
                    return {
                        "reauth_required": True,
                        "error": body.get("error", "Re-authentication required"),
                    }
            except Exception:
                pass
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[auth] token refresh failed: {e}")
        return None


def get_channel_rewards(
    broadcaster_id: str,
    access_token: str,
    client_id: str = "",
    only_manageable: bool = False,
) -> list[dict]:
    """
    Fetch custom channel-point rewards for the broadcaster.

    Proxied mode  (client_id == ""): calls Worker /rewards — Client-Id is added
    server-side; never exposed to the end-user machine.

    BYOI mode (client_id set): calls Twitch directly with the user's own app ID.

    only_manageable=True returns only rewards created by this app's Client-Id,
    which is the right filter when recovering from a duplicate-title conflict.

    Returns [] on any error (missing scope, network failure, etc.).
    Requires channel:manage:redemptions scope on the access token.
    """
    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/rewards"
            resp = requests.post(
                _url,
                json={
                    "access_token":    access_token,
                    "broadcaster_id":  broadcaster_id,
                    "only_manageable": only_manageable,
                },
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
        else:
            resp = requests.get(
                f"{TWITCH_API}/channel_points/custom_rewards",
                params={
                    "broadcaster_id": broadcaster_id,
                    "only_manageable_rewards": "true" if only_manageable else "false",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": client_id,
                },
                timeout=10,
            )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"[auth] rewards fetch failed: {e}")
        return []


def set_reward_enabled(
    broadcaster_id: str,
    reward_id: str,
    enabled: bool,
    access_token: str,
    client_id: str = "",
) -> bool:
    """
    Enable or disable a custom channel-point reward.

    Proxied mode  (client_id == ""): calls Worker /reward-toggle.
    BYOI mode (client_id set): calls Twitch directly.

    Returns True on success.  Requires channel:manage:redemptions scope.
    """
    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/reward-toggle"
            resp = requests.post(
                _url,
                json={
                    "access_token": access_token,
                    "broadcaster_id": broadcaster_id,
                    "reward_id": reward_id,
                    "is_enabled": enabled,
                },
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
        else:
            resp = requests.patch(
                f"{TWITCH_API}/channel_points/custom_rewards",
                params={"broadcaster_id": broadcaster_id, "id": reward_id},
                json={"is_enabled": enabled},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": client_id,
                },
                timeout=10,
            )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[auth] reward toggle failed: {e}")
        return False


def eventsub_subscribe(
    access_token:   str,
    broadcaster_id: str,
    session_id:     str,
    reward_id:      str = "",
    client_id:      str = "",
) -> bool:
    """
    Register a channel.channel_points_custom_reward_redemption.add subscription
    pointing at an EventSub WebSocket session.

    Proxied mode (client_id == ""): delegates to Worker /eventsub-subscribe.
    BYOI mode (client_id set):      calls Helix directly.

    Returns True on success (or if already subscribed — 409).
    """
    _SUB_TYPE = "channel.channel_points_custom_reward_redemption.add"

    condition: dict = {"broadcaster_user_id": broadcaster_id}
    if reward_id:
        condition["reward_id"] = reward_id

    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/eventsub-subscribe"
            resp = requests.post(
                _url,
                json={
                    "access_token":   access_token,
                    "broadcaster_id": broadcaster_id,
                    "session_id":     session_id,
                    "reward_id":      reward_id or None,
                },
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
        else:
            resp = requests.post(
                f"{TWITCH_API}/eventsub/subscriptions",
                json={
                    "type":      _SUB_TYPE,
                    "version":   "1",
                    "condition": condition,
                    "transport": {"method": "websocket", "session_id": session_id},
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id":     client_id,
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )
        return resp.status_code in (200, 202, 409)
    except Exception as e:
        print(f"[auth] eventsub_subscribe failed: {e}")
        return False


def update_redemption(
    broadcaster_id: str,
    redemption_id:  str,
    reward_id:      str,
    status:         str,   # "FULFILLED" | "CANCELED"
    access_token:   str,
    client_id:      str = "",
) -> bool:
    """
    Mark a channel-point redemption as FULFILLED or CANCELED (refund).

    Proxied mode (client_id == ""): delegates to Worker /fulfill-redemption.
    BYOI mode (client_id set):      calls Helix PATCH directly.

    Returns True on success.  Requires channel:manage:redemptions scope.
    """
    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/fulfill-redemption"
            resp = requests.post(
                _url,
                json={
                    "access_token":   access_token,
                    "broadcaster_id": broadcaster_id,
                    "redemption_id":  redemption_id,
                    "reward_id":      reward_id,
                    "status":         status,
                },
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
        else:
            resp = requests.patch(
                f"{TWITCH_API}/channel_points/custom_rewards/redemptions"
                f"?broadcaster_id={broadcaster_id}"
                f"&id={redemption_id}"
                f"&reward_id={reward_id}",
                json={"status": status},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id":     client_id,
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[auth] update_redemption failed: {e}")
        return False


def create_channel_points_reward(
    broadcaster_id: str,
    access_token: str,
    title: str = "Song Request",
    client_id: str = "",
) -> Optional[dict]:
    """
    Create a custom channel-point reward owned by this app's Client-Id.

    Only the app (Client-Id) that creates a reward can later PATCH its
    redemption status (FULFILLED / CANCELED).  Rewards created in the Twitch
    dashboard cannot be managed programmatically by third-party apps.

    Reward cost is hardcoded to 200 points.  Streamers can adjust it afterwards
    at https://dashboard.twitch.tv/u/{user}/viewer-rewards/channel-points/rewards

    Proxied mode (client_id == ""): calls Worker /create-reward.
    BYOI mode (client_id set):      calls Helix directly.

    Returns the reward dict on success, None on failure.
    Requires channel:manage:redemptions scope.
    """
    _COST = 200
    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/create-reward"
            resp = requests.post(
                _url,
                json={
                    "access_token":   access_token,
                    "broadcaster_id": broadcaster_id,
                    "title":          title,
                },
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
        else:
            resp = requests.post(
                f"{TWITCH_API}/channel_points/custom_rewards",
                params={"broadcaster_id": broadcaster_id},
                json={
                    "title":                  title,
                    "cost":                   _COST,
                    "is_user_input_required": True,
                    "prompt":                 "Enter a song URL or search query",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id":     client_id,
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )

        if resp.status_code in (200, 201):
            data = resp.json().get("data", [])
            return data[0] if data else None
        # Extract the most useful error detail from the response
        _detail = resp.text[:400]
        try:
            _j = resp.json()
            _detail = _j.get("message") or _j.get("error") or _detail
        except Exception:
            pass
        print(f"[auth] create_reward failed {resp.status_code}: {_detail}")
        raise RuntimeError(f"Twitch {resp.status_code}: {_detail}")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"[auth] create_reward error: {e}")
        raise RuntimeError(str(e)) from e


def check_reward_exists(
    broadcaster_id: str,
    reward_id: str,
    access_token: str,
    client_id: str = "",
) -> bool:
    """
    Return True if the reward still exists and is manageable by this app.

    Returns True on network / auth errors so a transient failure doesn't
    incorrectly clear the stored reward_id and warn the user.

    Proxied mode (client_id == ""): fetches the full manageable-rewards list
    from Worker /rewards and checks if reward_id is present.
    BYOI mode (client_id set):      calls Helix with ?id= filter and
    only_manageable_rewards=true.
    """
    import dpop_utils as _dpop
    try:
        if not client_id:
            from constants import TWITCH_WORKER_URL
            _url = f"{TWITCH_WORKER_URL}/rewards"
            resp = requests.post(
                _url,
                json={"access_token": access_token, "broadcaster_id": broadcaster_id},
                headers=_dpop.dpop_header("POST", _url, access_token),
                timeout=10,
            )
            if resp.status_code != 200:
                return True  # assume OK — transient error
            rewards = resp.json().get("data", [])
            return any(r.get("id") == reward_id for r in rewards)
        else:
            resp = requests.get(
                f"{TWITCH_API}/channel_points/custom_rewards",
                params={
                    "broadcaster_id":         broadcaster_id,
                    "id":                     reward_id,
                    "only_manageable_rewards": "true",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id":     client_id,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return True  # assume OK on unexpected error
            return bool(resp.json().get("data", []))
    except Exception:
        return True  # network error → assume OK


def issue_jwt(username: str, secret: str, expiry_minutes: int = 120) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + expiry_minutes * 60,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_jwt(token: str, secret: str) -> Optional[str]:
    """Returns the username on success, None on failure."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
