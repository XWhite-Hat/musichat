# MusicHat

A music player for Twitch streamers. Your chat requests songs, you see a live spectrogram, your mods control the queue from a browser panel — all without exposing a port or installing a server.

---

## Acknowledgement of AI

First and foremost I want to point out that I have used AI to help me create this project. My usual coding standards are so far removed from good practice that I decided instead of dumping a load of incoherent gibberish and calling it a project;
I opted to use AI as a way of making sure my code is legible enough for anyone to read and understand.
The core idea, implementation and percieved gaps in other applications I was filling were and are, entirely my own.

---

## What it does

- **Chat song requests** — viewers use commands to add YouTube or SoundCloud tracks. Per-user queue limits, a `wrongsong`-like undo command (with automatic channel point refunds), and a pause-requests toggle for the streamer.
- **Channel Points integration** — create a custom reward and MusicHat automatically fulfills (or refunds) it when the song plays or gets removed.
- **Mod panel** — a web panel your moderators open in any browser. They can skip, remove, and reorder the queue without being at your PC. Access is over your tunnel of choice (Cloudflare, ngrok, or Tailscale) and requires a Twitch login with mod verification.
- **Spectrogram overlay** — a real-time audio visualizer you add to OBS as a browser source. Multiple presets, fully configurable colours and shape.
- **Settings page** — a local web UI (localhost only, never tunnelled) for all configuration. No config files to hand-edit.

---

## Requirements

- Windows 10 or 11

Everything else is optional and only needed if you choose to use those features:

- **Twitch integration** — a Twitch account to sign into, and optionally a separate bot account for chat messages
- **Remote mod panel** — a tunnel binary: [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), [ngrok](https://ngrok.com/download), or [Tailscale](https://tailscale.com/download)

If you have music rights and only want the queue and spectrogram overlay for OBS, you can run MusicHat without signing into Twitch at all.

If you are running from source rather than the release binary, you also need:

- Python 3.14
- The dependencies in `requirements.txt`
- The vendored LGPL PyAV wheel (see [Building from source](#building-from-source))

---

## Getting started

1. Download `musichat-windows.exe` from the [Releases](../../releases) page. Verify the download if you want — the `.bundle` file and `SHA256SUMS.txt` are attached to every release (see [Verifying a download](#verifying-a-download)).
2. Run it. On first launch a setup wizard will ask you where to install PySide6 (the UI library). This is a one-time step — MusicHat downloads PySide6 to a folder you choose rather than bundling it, so the binary stays small and the LGPL terms are met.
3. Sign into Twitch when prompted. MusicHat opens your browser to Twitch's login page. No passwords go through MusicHat — see [How authentication works](#how-authentication-works).
4. (Optional) Sign a bot account in from the Settings page if you want chat messages to come from a separate account.
5. Add the spectrogram as a browser source in OBS: `http://localhost:8765/overlay`.

---

## How authentication works

MusicHat ships in **standalone mode** by default. In this mode a Cloudflare Worker deployed at `musicauth.xwhitehat.dev` acts as the OAuth middleman between your desktop and Twitch.

### Why a middleman?

Twitch's Authorization Code Grant (the secure OAuth flow) requires a `client_secret`. If that secret were baked into the desktop binary, anyone could extract it and impersonate the app. Instead, the secret lives only inside a Cloudflare Worker — a server-side process that MusicHat calls over HTTPS. The binary never sees the secret at any point.

The Worker source is not included in this repository. If you prefer not to trust a service you cannot inspect, the [BYOI mode](#bring-your-own-app-byoi-mode) section below explains how to route auth directly through your own Twitch application instead.

### What the Worker does

When you sign into Twitch through MusicHat, here is the exact sequence:

1. **MusicHat generates a random state value** (32 hex characters) and opens your browser to `musicauth.xwhitehat.dev/login?mode=streamer&state=<random>`.
2. **The Worker redirects your browser to Twitch** with the app's `client_id`, the required OAuth scopes, and your state value. Your browser talks directly to Twitch — the Worker is not a proxy here, just a redirect.
3. **You log in on Twitch's own page** and approve the permissions.
4. **Twitch redirects your browser back to the Worker** at `/callback` with a short-lived authorization code.
5. **The Worker exchanges the code for tokens** by calling Twitch's token endpoint server-side (this is the step that requires the secret). The tokens are stored in Cloudflare KV under a random UUID with a 60-second expiry and single-use lock. The tokens are never written to any URL or log.
6. **Your browser is redirected to `localhost:7329/token?code=<UUID>`** — the opaque UUID, not the tokens.
7. **MusicHat calls the Worker's `/exchange` endpoint** with the UUID over a local HTTP request. The Worker returns the tokens, then deletes them from KV immediately. After 60 seconds any uncollected token is auto-deleted regardless.
8. **Tokens are stored in Windows Credential Manager** on your machine. The `config.json` file stores only a sentinel marker (`"_keyring"`), never the token values.

After initial sign-in, token refresh works the same way: MusicHat calls `/refresh` on the Worker, which calls Twitch's refresh endpoint and returns new tokens. Your refresh token never touches any URL parameter.

### What the Worker does NOT do

- It does not store your tokens long-term. KV entries are deleted on first use or after 60 seconds, whichever comes first.
- It does not log token values. The Worker source code has no `console.log` of any token field.
- It does not have read access to your channel or any Twitch data beyond what is needed to verify your identity and mod status.
- It cannot play or queue music, it cannot modify your channel settings, and it has no access to anything that happens after the initial sign-in.

### DPoP binding (proof of possession)

Every call from the MusicHat desktop app to the Worker is bound to an EC P-256 keypair generated at startup and stored in Windows Credential Manager. This is an implementation of [RFC 9449 (DPoP)](https://datatracker.ietf.org/doc/html/rfc9449). Each request carries a signed proof that includes the HTTP method, URL, a timestamp, and a one-time ID (JTI) — the Worker verifies all of these. This means a captured network request cannot be replayed, and a token stolen from memory cannot be used from a different machine.

### Mod panel authentication (Channel B)

The mod panel is a separate authentication channel. When a moderator opens the panel URL in their browser:

1. They are redirected to Twitch to log in with their own account.
2. The Worker verifies they are a moderator of your channel.
3. A short-lived session JWT is issued and stored only in their browser's session memory.
4. The moderator's browser generates its own EC P-256 keypair and registers the public key with your local MusicHat server. Subsequent requests are DPoP-bound to that keypair.

Your streamer token is never involved in mod authentication.

### Bring your own app (BYOI mode)

If you prefer not to use the shared Worker, you can register your own Twitch application and supply both credentials in a `.env` file. In BYOI mode MusicHat uses Twitch's Authorization Code flow directly — the Worker is not contacted at all, and a refresh token is issued so you never need to re-authenticate when the access token expires.

#### 1. Register your Twitch application

Go to [dev.twitch.tv/console](https://dev.twitch.tv/console) → **Register Your Application**:

- **Name:** anything (e.g. `My MusicHat`)
- **Category:** Application Integration
- **OAuth Redirect URIs:** add **both** of the following:
  - `http://localhost:7329/callback` — used when you sign in as streamer or bot in the Settings page
  - `http://localhost:8765/auth/callback` — used when mods sign into the mod panel from a local browser. If you expose the panel through a tunnel (Cloudflare, ngrok, Tailscale), also add `https://<your-tunnel-domain>/auth/callback` to this list

#### 2. Create your `.env` file

**Running the release binary:** open `%LOCALAPPDATA%\musichat\` (or wherever you chose to install the data folder) and edit the `.env` file that MusicHat created there on first launch.

**Running from source:** copy `.env.example` to `.env` at the project root.

Uncomment and fill in both values:

```env
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here
```

The Client ID and Client Secret are both shown in the Twitch developer console under your application. The secret is stored only in this file on your own machine — it is never sent to the shared Worker or any MusicHat server.

#### 3. Start MusicHat

Restart MusicHat after saving `.env`. The Settings page will show an **Own App** badge next to your Twitch connection status to confirm BYOI mode is active. Sign in from there as normal.

#### Notes

- The Worker is not contacted in BYOI mode. DPoP binding is not applied.
- Tokens are still stored in Windows Credential Manager — the `.env` file holds only your app credentials, not your Twitch access or refresh tokens.
- The mod panel auth flow uses Twitch's Implicit Grant in all modes (including BYOI) because it is a pure browser-side flow. The streamer/bot auth uses full Authorization Code flow with the refresh token.

---

## Verifying a download

Every release binary is signed with [Sigstore](https://sigstore.dev/) keyless signing. No private key to trust — the signature is bound to the GitHub Actions workflow identity.

```bash
cosign verify-blob \
  --certificate-identity \
    "https://github.com/xwhitehat/musichat/.github/workflows/release.yml@refs/tags/v<version>" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --bundle musichat-windows.exe.bundle \
  musichat-windows.exe
```

The `SHA256SUMS.txt` file attached to each release contains the SHA-256 hash of every binary if you prefer a simpler check.

---

## Building from source

```powershell
# Clone and create a venv
python -m venv .venv

# Install dependencies.
# Use the hash-pinned lockfile for supply-chain protection equivalent to the
# release binary (recommended); use requirements.txt for fast iteration only.
.venv\Scripts\pip install --require-hashes --no-deps -r requirements-build.txt
# or: .venv\Scripts\pip install -r requirements.txt

# Install the LGPL-only PyAV wheel (replaces the PyPI av wheel which bundles GPL codecs)
.venv\Scripts\pip install --force-reinstall --no-deps vendor/av-17.1.0-cp314-cp314-win_amd64.whl

# Run from source
python main.py

# Build the release binary
.\scripts\build.ps1
```

The build script automatically reinstalls the LGPL wheel before running PyInstaller, so the release binary contains only LGPL FFmpeg — no GPL x264 or x265.

### Why the vendored wheel exists

The `av` package on PyPI is a Python wrapper around FFmpeg (BSD-3 licensed). The problem is the prebuilt PyPI wheel bundles `libx264` and `libx265` as hard DLL imports — both GPL-licensed. Installing it from PyPI means the binary ships GPL code, which is incompatible with MIT distribution.

`vendor/av-17.1.0-cp314-cp314-win_amd64.whl` is a rebuild of PyAV 17.1.0 against [BtbN's FFmpeg 8.1 LGPL shared build](https://github.com/BtbN/FFmpeg-Builds) — a well-maintained set of prebuilt FFmpeg binaries compiled with `--disable-gpl`, `--disable-libx264`, `--disable-libx265`. The resulting wheel contains only LGPL FFmpeg libraries (`avcodec`, `avformat`, `avutil`, `swresample`, `swscale`, `avfilter`, `avdevice`).

### Rebuilding the wheel

If you upgrade PyAV or move to a new Python version, `scripts/build-lgpl-av.ps1` automates the rebuild. In short, it:

1. Downloads the BtbN LGPL FFmpeg shared build for Windows
2. Builds PyAV from source against those libraries with `python setup.py bdist_wheel`
3. Outputs the wheel to `vendor/`

You need a C compiler (Visual Studio Build Tools) and the matching CPython version. The script handles the rest. See the comments inside the script for the exact BtbN release URL to target.

---

## Tunnel setup

The mod panel is exposed through a tunnel you run locally. Pick one:

| Mode | Setup |
|---|---|
| Cloudflare | Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) and select **Cloudflare** in Settings → Server |
| ngrok | Install [ngrok](https://ngrok.com/download), authenticate with `ngrok config add-authtoken <token>`, select **ngrok** in Settings → Server |
| Tailscale | Install [Tailscale](https://tailscale.com/download), join the tailnet, select **Tailscale** in Settings → Server |
| None | Leave tunnel off if you only need the local settings page and OBS overlay |

The tunnel URL changes each session with Cloudflare free tunnels and ngrok free accounts. Tailscale gives a stable URL.

---

## Music rights

MusicHat uses yt-dlp to resolve audio streams at URLs you or your viewers provide. It does not supply music or any rights to it. Public performance of copyrighted music on a live stream — including through this app — requires a license from the relevant performing rights organisation in your jurisdiction (ASCAP, BMI, SESAC, SOCAN, PRS for Music, APRA AMCOS, or equivalent). See the [LICENSE](LICENSE) file for the full statement.

Vibe mode queues tracks selected by YouTube's recommendation algorithm. Because the app is choosing content rather than you, a one-time consent prompt appears the first time you enable it. You can review what it selected in the queue at any time. This is only enable-able after you have a song playing or enable it when selecting a playlist you have already made. From that point it will pin the song/playlist and select based on that pin. You are entirely responsible for overviewing your own queue and making sure you have the rights to reproduce that content, in whatever context you are reproducing it in.

---

## Contributing

If you do send a pull request, a few things to know:

- **CI must pass.** The pipeline runs ruff (linting), pip-audit (CVE check), zizmor (workflow security), and a client-ID leak scan. Run `ruff check .` locally before pushing.
- **No bundled GPL binaries.** The CI dependency review blocks GPL and AGPL packages outright. LGPL in a dependency's license metadata is fine — this project already ships LGPLv2.1+ (FFmpeg) and loads LGPLv3 (PySide6) at runtime. The problem is different: some PyPI wheels are clean on paper but embed GPL-licensed native libraries inside the bundled DLLs. `av` is why this project vendors a custom wheel — the PyPI build silently pulled in x264 and x265. That fix was feasible because BtbN maintains production LGPL FFmpeg builds and PyAV is explicitly designed to link against them. Most packages with bundled native code have no equivalent path. If an addition requires a custom wheel to be distribution-clean, it won't be merged.
- **No secrets in source.** `TWITCH_APP_CLIENT_ID` must never be committed. Use `.env` for local BYOI development.
- **Thread safety.** Qt widgets must only be touched from the Qt main thread. Cross-thread communication goes through `QTimer.singleShot(0, fn)` or `queue.Queue`.
- **The settings API is restricted.** Fields in `BLOCKED_PATCH` (`server/settings_app.py`) cannot be written via `PATCH /api/config`. Adding a new sensitive field? Add it to the set.

---

## License

[MIT](LICENSE) — see the "Intended Use and Media Rights" section in that file for the yt-dlp and music rights statement.

Third-party license attributions are in [NOTICE](NOTICE).
