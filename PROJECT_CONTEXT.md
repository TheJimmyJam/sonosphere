# PROJECT_CONTEXT — Sonosphere

## What it is
Sonosphere is a macOS desktop app that lets you search YouTube, manage a local audio library, and stream music to Sonos speakers on your home network. It runs as a local Flask server and opens in Chrome as a standalone app window. Tracks can be downloaded for offline/instant playback or streamed live (no download required) via a Flask proxy that resolves YouTube audio URLs on the fly and pipes them to Sonos over UPnP.

## Status
Active development — core playback, library, streaming, and YouTube playlist integration working. In-progress bugfixing on live streaming format compatibility.

## Stack
| Layer | Tech | Host |
|---|---|---|
| Frontend | Vanilla JS + CSS (single HTML file) | Local Flask |
| Backend | Python 3 + Flask | Local (port 8888) |
| Audio download | yt-dlp + pytubefix (fallback) | — |
| Sonos control | soco (UPnP) | LAN |
| YouTube auth | Chrome cookie export (Netscape format) | — |
| Source control | Git | GitHub: https://github.com/TheJimmyJam/sonosphere |
| Launcher | `Start Sonosphere.command` (double-click) | Local |

## Repo layout
```
Sonosphere/
  app.py                  Flask backend — all routes, yt-dlp, Sonos, streaming proxy
  Start Sonosphere.command  Double-click launcher (sets up venv, upgrades yt-dlp, starts app)
  requirements.txt
  templates/
    index.html            Entire frontend (single file — all JS/CSS inline)
  skins/                  156 preset visual themes (each: skin.json, theme.css, preview.svg)
  assets-logos/           Sonosphere logo PNGs
  venv/                   Python virtualenv (not committed)
```

## Credentials
- `GITHUB_PAT_CLASSIC` / `GITHUB_PAT_FINE_GRAINED` — push via GitHub API (no git CLI)
- GitHub: https://github.com/TheJimmyJam/sonosphere

## Features built
- YouTube search (yt-dlp, 15 results)
- Download to local library (`~/Music/SonosPlayer/`) with background polling
- Live streaming proxy — play any YouTube track without downloading (Flask → Sonos)
- Sonos room discovery, volume control, group/unjoin
- Play queue with add, remove, shuffle, skip, persist across restarts (`.queue_state.json`)
- YouTube playlist browsing via yt-dlp + Chrome cookies (`/feed/playlists`, playlist tracks, Liked Videos)
- 156 switchable skins (CSS variable themes with previews)
- Fullscreen visualizer tab (Web Audio API)
- Sonosphere logo in topbar
- macOS SSL fix via certifi for pytubefix
- Cookie export at startup (Netscape format) for yt-dlp auth

## Pending / next up
- Live streaming stability — format selection (AAC/M4A only, WebM causes UPnP 714 error from Sonos)
- Auto-advance queue when track finishes
- YouTube Music playlists vs YouTube playlists distinction (user has YouTube playlists, not YTM)
- Better error UX (retry button, clearer messages)

## Notes
- **Deploy via GitHub API only** — no git CLI in the project folder (no `.git` dir locally)
- **Cookie file** (`~/Music/SonosPlayer/yt_cookies.txt`) is exported at startup from Chrome via subprocess. `cookiesfrombrowser` called inside Flask fails on macOS (Keychain not accessible). Always use the cookie FILE for yt-dlp calls inside Flask routes; only use `cookiesfrombrowser` for the `/api/ytm/playlists` routes which need SAPISIDHASH auth.
- **Sonos rejects WebM/Opus** with UPnP error 714. Force `bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]` for all streaming.
- **yt-dlp auto-updates** on every launch via `pip install --upgrade yt-dlp` in the `.command` file — YouTube changes extraction frequently.
- **Library path**: `~/Music/SonosPlayer/` — audio files named `{video_id}.{ext}`, index at `.index.json`
- **Skins**: each skin lives in `skins/{skin-id}/` with `skin.json` (metadata), `theme.css` (CSS vars), optional `preview.svg`
- **ytmusicapi** is still in requirements but only used as dead code — all playlist/liked routes now use yt-dlp directly
