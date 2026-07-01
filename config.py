from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from data_dir import DATA_DIR as _DATA_DIR
CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")


@dataclass
class SpectrogramConfig:
    name: str = "Main"                     # preset identifier — must be unique

    # --- Colour ---
    color_preset: str = "matrix"           # matrix | fire | cyan | purple | sunset | custom
    color_start: str = "#003300"           # gradient low-intensity colour
    color_mid: str = "#00cc33"             # gradient mid colour (used by some presets)
    color_end: str = "#00ff41"             # gradient high-intensity colour
    background_color: str = "#000000"
    background_alpha: int = 255            # 0 = fully transparent (OBS mode), 255 = opaque
    # When True, the gradient is auto-set from the track's cover art each time
    # a new track starts.  colour_start/mid/end are overwritten in memory (and
    # saved) so the last-used palette persists if the feature is toggled off.
    cover_art_match: bool = False

    # --- Bars ---
    bar_count: int = 64                    # number of frequency bars rendered
    bar_gap: int = 2                       # pixels between bars
    bar_min_height: int = 2               # floor height in px so flat bars are still visible

    # --- Geometry ---
    camber_degrees: float = 0.0            # arc curvature: 0=flat, 360=full circle
    camber_asymmetric: bool = False        # ouroboros: first and last bars blend seamlessly
    double_sided: bool = False             # bars extend symmetrically from centre line
    inverted: bool = False                 # bars hang/point inward instead of outward

    # --- Browser Source canvas size ---
    obs_width: int = 800
    obs_height: int = 200

    # --- Frequency window ---
    freq_min: int = 20                     # Hz
    freq_max: int = 20000                  # Hz

    # --- Peak hold ---
    peak_hold: bool = True
    peak_hold_frames: int = 45             # frames before decay starts
    peak_decay_rate: float = 0.015         # magnitude lost per frame after hold expires

    # --- Rendering ---
    vis_mode: str = "bar"                 # bar | waterfall | line
    window_function: str = "hann"         # hann | blackman | hamming
    fps_target: int = 60
    smoothing: float = 0.75               # temporal smoothing (0 = none, 1 = frozen)
    fft_size: int = 2048                   # FFT window size (power of 2)


@dataclass
class TierConfig:
    """Per-viewer-tier request permissions for chat-command requests."""
    can_request: bool = True
    queue_limit: int = 5          # max songs in queue per user at once; 0 = unlimited
    cooldown_seconds: int = 30    # per-user cooldown between requests; 0 = none
    paid_request_limit: int = 0   # max channel-points requests per stream; 0 = unlimited
    max_per_stream: int = 0       # max total chat requests per stream per user; 0 = unlimited


@dataclass
class TwitchConfig:
    # ── Streamer account ────────────────────────────────────────────────────────
    streamer_token: str = ""               # bare access token (no oauth: prefix)
    streamer_username: str = ""
    streamer_id: str = ""
    channel: str = ""                      # auto-populated from streamer sign-in

    # ── Bot account ─────────────────────────────────────────────────────────────
    # By default the app chats as the streamer.  Set use_separate_bot to use a
    # dedicated account (e.g. "StreamerMusicBot").
    use_separate_bot: bool = False
    bot_token: str = ""                    # bare access token — only set when use_separate_bot
    bot_username: str = ""
    bot_id: str = ""                       # Twitch user_id for the bot account

    # ── Refresh tokens ────────────────────────────────────────────────────────
    # Populated in both proxied (Worker Auth Code) and BYOI (direct Auth Code)
    # modes.  Empty only if the user authenticated before BYOI was upgraded from
    # Implicit Grant — a fresh sign-in will populate them.
    streamer_refresh_token: str = ""
    bot_refresh_token: str = ""

    # ── Token expiry tracking ─────────────────────────────────────────────────
    # Unix timestamp of when each access token was last issued / refreshed.
    # 0.0 means "unknown" — treated as stale so a refresh is attempted once.
    # expires_in is seconds from issuance (Twitch default: 14400 = 4 hours).
    # Used to avoid hammering /refresh on every cold start — only refresh when
    # the token is within REFRESH_BUFFER_SECONDS of actual expiry.
    streamer_token_issued_at: float = 0.0
    streamer_token_expires_in: int = 14400
    bot_token_issued_at: float = 0.0
    bot_token_expires_in: int = 14400

    # ── Proxied-auth consent tracking ────────────────────────────────────────────
    # True after the user reads and accepts the proxy-auth notice.
    # Persists across restarts so the one-time consent isn't shown again.
    proxied_consent_given: bool = False

    # ── Developer override ───────────────────────────────────────────────────────
    # When non-empty, overrides TWITCH_CLIENT_ID env var.
    # Only needed if you want a different client ID per-project vs the global .env.
    client_id: str = ""

    # ── Chat command settings ────────────────────────────────────────────────────
    prefix: str = "!"
    skip_permission: str = "mod"           # mod | vip | subscriber | all

    # ── Blocklists ───────────────────────────────────────────────────────────────
    blacklist_channels: list = field(default_factory=list)
    blacklist_terms: list = field(default_factory=list)

    # ── Per-tier request rules ────────────────────────────────────────────────────
    # Tiers in descending privilege order: head_mod > mod > vip > subscriber > viewer
    # Each tier has independent can_request / queue_limit / cooldown_seconds settings.
    tier_head_mod: TierConfig = field(
        default_factory=lambda: TierConfig(
            can_request=True, queue_limit=0, cooldown_seconds=0, paid_request_limit=0
        )
    )
    tier_mod: TierConfig = field(
        default_factory=lambda: TierConfig(
            can_request=True, queue_limit=0, cooldown_seconds=0, paid_request_limit=0
        )
    )
    tier_vip: TierConfig = field(
        default_factory=lambda: TierConfig(
            can_request=True, queue_limit=10, cooldown_seconds=15, paid_request_limit=5
        )
    )
    tier_subscriber: TierConfig = field(
        default_factory=lambda: TierConfig(
            can_request=True, queue_limit=5, cooldown_seconds=30, paid_request_limit=3
        )
    )
    tier_viewer: TierConfig = field(
        default_factory=lambda: TierConfig(
            can_request=True, queue_limit=3, cooldown_seconds=60, paid_request_limit=1
        )
    )

    # ── Chat command aliases ──────────────────────────────────────────────────────
    # Maps command name → list of extra alias strings (without prefix).
    # Example: {"songrequest": ["req", "play"], "wrongsong": ["oops"]}
    # Built-in aliases (sr, request, q, song, np, etc.) are always active;
    # entries here are additive.
    command_aliases: dict = field(default_factory=dict)

    # ── Channel Points integration ────────────────────────────────────────────────
    channel_points_enabled: bool = False
    channel_points_reward_id: str = ""
    # Note: reward cost is hardcoded to 200 pts on creation — adjust on Twitch
    # dashboard afterwards: https://dashboard.twitch.tv/u/{user}/viewer-rewards/…
    channel_points_cooldown_seconds: int = 0  # global reward cooldown; 0 = none
    requests_paused: bool = False               # pauses chat commands + CP reward


@dataclass
class YouTubeConfig:
    # Vibe-match auto-fill — controlled by the toggle in the main UI, not stored here.
    suggestion_threshold: int = 3          # start auto-filling after N tracks play
    suggestion_count: int = 5              # tracks to fetch per vibe cycle

    # Vibe rigidness — 0.0 = max diversity (strong artist-repeat penalty),
    # 1.0 = strict (trust YouTube radio signal, no extra artist penalty).
    vibe_rigidness: float = 0.7

    # Artist guard — when True, apply an extra penalty to artists that dominate
    # the fetched suggestion batch, pushing diversity in dense-genre situations.
    vibe_artist_guard: bool = True


@dataclass
class SoundCloudConfig:
    client_id: str = ""


@dataclass
class ServerConfig:
    port: int = 8765
    host: str = "127.0.0.1"
    jwt_secret: str = ""                   # auto-generated on first run if empty
    jwt_expiry_minutes: int = 120
    tunnel_mode: str = "none"             # none | cloudflare | ngrok | tailscale | self
    ngrok_authtoken: str = ""
    ngrok_domain: str = ""
    cloudflare_accepted_tos: bool = False


@dataclass
class AudioConfig:
    output_device: Optional[int] = None   # None = system default
    sample_rate: int = 48000
    channels: int = 2
    blocksize: int = 1024
    volume: float = 0.8                   # linear 0–1; persisted across sessions


@dataclass
class OverlayConfig:
    """
    Configuration for the OBS browser-source overlays.

    nowplaying_template supports these variables:
      {title}        — track title (artist prefix stripped if duplicated)
      {artist}       — artist name (empty string if unknown)
      {display}      — "{artist} — {title}" or just title when no artist
      {requested_by} — Twitch username of requester, or empty string
      {source}       — youtube | soundcloud | local
      {duration}     — mm:ss formatted duration

    nowplaying_template_requested — used *instead* of nowplaying_template when
      the track has a non-empty requested_by (chat !sr or channel points).
      {requested_by} is guaranteed to be filled here.
    """
    nowplaying_template: str = "{display}"
    nowplaying_template_requested: str = "{display} — requested by @{requested_by}"
    albumart_size: int = 512   # pixel dimensions for square/circle renders

    # ── Text overlay (OBS browser source) ─────────────────────────────────────
    # Set your OBS browser source width to text_width and height to
    # ceil(text_font_size * 1.4) + 12  (the settings page shows the exact value).
    text_font_size: int   = 22
    text_width:     int   = 600
    text_color:     str   = "#ffffff"
    text_scroll:    bool  = False   # scroll overflowing text every 10 s
    # CSS font-family value.  One of the preset names or any CSS font stack.
    text_font:       str  = "Share Tech Mono"
    # Optional @import URL (e.g. a Google Fonts link).  Injected into the
    # overlay page so custom fonts load without a local install.
    text_font_import: str = "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap"


@dataclass
class AppConfig:
    # Spectrogram presets — list of SpectrogramConfig (serialised as list of dicts).
    # Use the `spectrogram` property for backward-compat single-preset access.
    spectrogram_presets: list = field(
        default_factory=lambda: [SpectrogramConfig()]
    )
    active_preset_name: str = "Main"
    overlay_fps: int = 30                   # browser-source spectrogram push rate (fps)

    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    soundcloud: SoundCloudConfig = field(default_factory=SoundCloudConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    window_geometry: str = ""              # saved as base64 QByteArray
    theme: str = "xwhitehat"
    vibe_ack: bool = False                 # cleared on data-wipe; re-shows vibe consent dialog

    # ── Backward-compat / convenience ──────────────────────────────────────────

    @property
    def spectrogram(self) -> SpectrogramConfig:
        """Return the active preset.  Never returns None — falls back to first."""
        return self.get_preset(self.active_preset_name) or self.spectrogram_presets[0]

    def get_preset(self, name: str) -> Optional[SpectrogramConfig]:
        for p in self.spectrogram_presets:
            if p.name == name:
                return p
        return None

    def add_preset(self, name: str) -> SpectrogramConfig:
        """Create a new preset (copy of defaults) and append it."""
        p = SpectrogramConfig(name=name)
        self.spectrogram_presets.append(p)
        return p

    def delete_preset(self, name: str) -> None:
        """Remove a preset by name; adjusts active_preset_name if needed."""
        self.spectrogram_presets = [
            p for p in self.spectrogram_presets if p.name != name
        ]
        if not self.spectrogram_presets:
            self.spectrogram_presets = [SpectrogramConfig()]
        if self.active_preset_name == name:
            self.active_preset_name = self.spectrogram_presets[0].name


def _dict_to_dataclass(cls, d: dict):
    """Recursively hydrate nested dataclasses from a dict."""
    if not isinstance(d, dict):
        return d
    hints = {f.name: f for f in cls.__dataclass_fields__.values()}
    kwargs = {}
    for key, val in d.items():
        if key not in hints:
            continue
        field_type = hints[key].type
        # Resolve string annotations
        if isinstance(field_type, str):
            import sys
            field_type = eval(field_type, sys.modules[cls.__module__].__dict__)
        if hasattr(field_type, "__dataclass_fields__") and isinstance(val, dict):
            kwargs[key] = _dict_to_dataclass(field_type, val)
        else:
            kwargs[key] = val
    return cls(**kwargs)


def _resolve_secrets(cfg: "AppConfig") -> None:
    """Replace sentinel values with the real secrets from the OS credential store."""
    import secure_store as _ss
    cfg.twitch.streamer_token         = _ss.resolve(cfg.twitch.streamer_token,         _ss.STREAMER_TOKEN)
    cfg.twitch.streamer_refresh_token = _ss.resolve(cfg.twitch.streamer_refresh_token, _ss.STREAMER_REFRESH)
    cfg.twitch.bot_token              = _ss.resolve(cfg.twitch.bot_token,              _ss.BOT_TOKEN)
    cfg.twitch.bot_refresh_token      = _ss.resolve(cfg.twitch.bot_refresh_token,      _ss.BOT_REFRESH)
    cfg.server.jwt_secret             = _ss.resolve(cfg.server.jwt_secret,             _ss.JWT_SECRET)


def _store_secrets(cfg: "AppConfig") -> None:
    """Move plain-text secrets to the OS credential store and replace with sentinels."""
    import secure_store as _ss
    cfg.twitch.streamer_token         = _ss.store(_ss.STREAMER_TOKEN,  cfg.twitch.streamer_token)
    cfg.twitch.streamer_refresh_token = _ss.store(_ss.STREAMER_REFRESH, cfg.twitch.streamer_refresh_token)
    cfg.twitch.bot_token              = _ss.store(_ss.BOT_TOKEN,        cfg.twitch.bot_token)
    cfg.twitch.bot_refresh_token      = _ss.store(_ss.BOT_REFRESH,      cfg.twitch.bot_refresh_token)
    cfg.server.jwt_secret             = _ss.store(_ss.JWT_SECRET,       cfg.server.jwt_secret)


def load_config() -> AppConfig:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        # ── Migrate old flat Twitch request rules → per-tier ─────────────────
        tw = raw.get("twitch", {})
        if isinstance(tw, dict) and "request_cap_non_sub" in tw:
            old_cap = tw.pop("request_cap_non_sub", 5)
            old_cd  = tw.pop("request_cooldown_seconds", 30)
            # Fold old globals into subscriber and viewer defaults; higher tiers
            # keep their more generous defaults.
            tw.setdefault("tier_subscriber", {}).setdefault("queue_limit", old_cap)
            tw.setdefault("tier_subscriber", {}).setdefault("cooldown_seconds", max(old_cd // 2, 15))
            tw.setdefault("tier_viewer",     {}).setdefault("queue_limit", old_cap)
            tw.setdefault("tier_viewer",     {}).setdefault("cooldown_seconds", old_cd)

        # ── Migrate old single-spectrogram format → presets list ──────────────
        if "spectrogram" in raw and "spectrogram_presets" not in raw:
            spec_dict = raw.pop("spectrogram")
            spec_dict.setdefault("name", "Main")
            raw["spectrogram_presets"] = [spec_dict]
            raw.setdefault("active_preset_name", spec_dict.get("name", "Main"))

        cfg = _dict_to_dataclass(AppConfig, raw)

        # ── Sanitise optional-int fields that may have been stored as "" ──────
        # Older versions of _set_typed didn't unwrap Optional[int], so the
        # settings server could save an empty string instead of null.
        if not isinstance(cfg.audio.output_device, int):
            cfg.audio.output_device = None

        # ── Post-process presets list: convert any remaining dicts ────────────
        # _dict_to_dataclass can't auto-convert list[SpectrogramConfig] since
        # the field type annotation is plain `list`.  We handle it here.
        coerced: list[SpectrogramConfig] = []
        for p in cfg.spectrogram_presets:
            if isinstance(p, SpectrogramConfig):
                coerced.append(p)
            elif isinstance(p, dict):
                coerced.append(_dict_to_dataclass(SpectrogramConfig, p))
        cfg.spectrogram_presets = coerced if coerced else [SpectrogramConfig()]

        _resolve_secrets(cfg)
        return cfg
    except Exception:
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    import copy
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    # Work on a shallow copy so we don't mutate live cfg with sentinels.
    cfg_copy = copy.deepcopy(cfg)
    _store_secrets(cfg_copy)
    # After _store_secrets, the original cfg still has real values in memory;
    # cfg_copy has sentinels (or plain values on keyring fallback).
    # Resolve sentinels back into cfg so in-memory state stays correct.
    _resolve_secrets(cfg)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        # asdict() recurses into SpectrogramConfig objects inside the list.
        # The `spectrogram` @property is not a dataclass field → not serialised.
        json.dump(asdict(cfg_copy), fh, indent=2)
