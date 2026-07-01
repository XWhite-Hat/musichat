"""
App-level constants.

Auth modes
──────────
This app supports two authentication modes:

  BYOI  (Bring Your Own ID) — set TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET
  ────────────────────────────────────────────────────────────────────────
  For open-source contributors or self-hosters who want to register their own
  Twitch application.

  Setup:
    1. Go to https://dev.twitch.tv/console → "Register Your Application"
    2. Name: anything (e.g. "My MusicHat")
    3. OAuth Redirect URI: http://localhost:7329/callback
    4. Category: "Application Integration"
    5. Create a .env file in the project root with both values:
         TWITCH_CLIENT_ID=your_client_id_here
         TWITCH_CLIENT_SECRET=your_client_secret_here
    6. The app will use Auth Code flow with your own app — no proxy involved.
       A refresh token is issued, so re-authentication is not required on expiry.

  In BYOI mode the consent dialog is suppressed (you control the app).

  PROXIED  (default, no .env required) — routes through musicauth.xwhitehat.dev
  ─────────────────────────────────────────────────────────────────────────────
  End users who download the binary get Auth Code flow via the developer's
  Cloudflare Worker.  Tokens are exchanged server-side; no client secret is
  bundled in the binary.  A consent dialog explains the proxy before sign-in.

Why a fixed port?
  Twitch requires an exact redirect URI match.  Port 7329 is registered once.
"""

import os as _os

# ── Auth mode detection ────────────────────────────────────────────────────────
# BYOI: user has set TWITCH_CLIENT_ID in their environment / .env file.
# Proxied: default — use the Cloudflare Worker at TWITCH_WORKER_URL.

TWITCH_APP_CLIENT_ID:     str = _os.environ.get("TWITCH_CLIENT_ID",     "")
TWITCH_APP_CLIENT_SECRET: str = _os.environ.get("TWITCH_CLIENT_SECRET", "")


def is_byoi_mode() -> bool:
    """True when the user is running their own registered Twitch application."""
    return bool(TWITCH_APP_CLIENT_ID)


# ── Proxied flow ───────────────────────────────────────────────────────────────
# Cloudflare Worker that handles Auth Code exchange server-side.
# Safe to commit — not a secret.
TWITCH_WORKER_URL: str = "https://musicauth.xwhitehat.dev"

# ── OAuth redirect (used by BYOI Auth Code flow) ──────────────────────────────
# Must match EXACTLY what is registered in the Twitch developer console.
TWITCH_REDIRECT_PORT: int = 7329
TWITCH_REDIRECT_URI:  str = f"http://localhost:{TWITCH_REDIRECT_PORT}/callback"

# ── OAuth scopes ───────────────────────────────────────────────────────────────
# The Worker owns the scope list for proxied mode.  These constants are used
# only in BYOI mode (Auth Code flow direct to Twitch).

TWITCH_STREAMER_SCOPES: str = (
    "moderator:read:moderators "   # read mod list
    "channel:read:vips "           # read VIP list
    "channel:read:subscriptions "  # check subscription status
    "channel:manage:redemptions "  # read + enable/disable channel-point rewards
    "user:read:email "             # basic identity
    # Chat — included so the streamer token works when no dedicated bot account
    # is configured (cfg.use_separate_bot == False).
    "user:read:chat "  # EventSub channel.chat.message (twitchio v3)
    "chat:read "       # IRC compat / legacy
    "chat:edit "
    "user:write:chat "
    "channel:moderate"
)

TWITCH_BOT_SCOPES: str = (
    "user:read:chat "  # EventSub channel.chat.message (twitchio v3)
    "chat:read "       # IRC compat / legacy
    "chat:edit "
    "user:write:chat " # send messages (Helix API)
    "channel:moderate" # timeouts, bans
)
