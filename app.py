import os
import socket
import json
import time
import threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, redirect
import yt_dlp
import soco

app = Flask(__name__)
PORT = 8888

# ── Skins pack (ships alongside app.py in a skins/ folder) ───────────────────
SKINS_DIR   = Path(__file__).parent / "skins"
ASSETS_DIR  = Path(__file__).parent / "assets-logos"

# ── Library folder (permanent, in your Music directory) ───────────────────────
LIBRARY_DIR = Path.home() / "Music" / "SonosPlayer"
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_INDEX = LIBRARY_DIR / ".index.json"         # maps video_id -> track metadata
SPOTIFY_TOKEN_FILE = LIBRARY_DIR / ".spotify_tokens.json"
PYTUBEFIX_TOKEN_FILE = str(LIBRARY_DIR / "oauth_token.json")  # shared OAuth token

# ── Download queue ────────────────────────────────────────────────────────────
download_status = {}   # video_id -> {"state": "downloading"|"done"|"error", "msg": "..."}
download_lock   = threading.Lock()

# ── Play queue (server-side for auto-advance tracking) ────────────────────────
play_queue      = []   # list of track dicts
play_queue_idx  = -1   # current position
queue_lock      = threading.Lock()
QUEUE_STATE_FILE = LIBRARY_DIR / ".queue_state.json"

def save_queue_state():
    """Persist queue + current index to disk (called after every mutation)."""
    try:
        QUEUE_STATE_FILE.write_text(json.dumps(
            {"queue": play_queue, "current": play_queue_idx}, indent=2
        ))
    except Exception:
        pass

def load_queue_state():
    """Restore queue from disk at startup."""
    global play_queue, play_queue_idx
    if not QUEUE_STATE_FILE.exists():
        return
    try:
        state = json.loads(QUEUE_STATE_FILE.read_text())
        with queue_lock:
            play_queue  = state.get("queue", [])
            play_queue_idx = state.get("current", -1)
        if play_queue:
            print(f"  ✓ Queue restored: {len(play_queue)} tracks (position {play_queue_idx})")
    except Exception as e:
        print(f"  ⚠ Could not restore queue: {e}")

# ── Spotify config (credentials loaded from ~/.credentials, never hardcoded) ──
def _load_credentials():
    """Read key=value pairs from ~/Desktop/Projects/.credentials"""
    creds = {}
    p = Path.home() / "Desktop" / "Projects" / ".credentials"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                creds[k.strip()] = v.strip()
    return creds

_creds = _load_credentials()
LASTFM_API_KEY        = _creds.get("SONOSPHERE_LASTFM_API_KEY", "")
SPOTIFY_CLIENT_ID     = _creds.get("SONOSPHERE_SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = _creds.get("SONOSPHERE_SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = _creds.get("SONOSPHERE_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/auth/spotify/callback")
SPOTIFY_SCOPES        = "user-library-read playlist-read-private playlist-read-collaborative user-read-private"
SPOTIFY_TOKEN_FILE    = None  # set after LIBRARY_DIR is created below
_spotify_yt_cache     = {}    # sp_<spotify_id> → youtube_video_id

# ── ytmusicapi client (lazy init) ─────────────────────────────────────────────
_ytm_client     = None
_ytm_lock       = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

LOCAL_IP = get_local_ip()


def load_index():
    if LIBRARY_INDEX.exists():
        try:
            return json.loads(LIBRARY_INDEX.read_text())
        except Exception:
            pass
    return {}

def save_index(index):
    LIBRARY_INDEX.write_text(json.dumps(index, indent=2))

def find_track_file(video_id):
    """Find the audio file for a video_id in the library."""
    for ext in ("m4a", "mp4", "mp3", "webm", "ogg"):
        p = LIBRARY_DIR / f"{video_id}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


# ── Cookie setup (once at startup) ───────────────────────────────────────────
import subprocess, sys

_COOKIE_FILE = str(LIBRARY_DIR / "yt_cookies.txt")

def _init_cookies():
    for browser in ("chrome", "safari", "firefox"):
        try:
            print(f"  Exporting cookies from {browser}…")
            subprocess.run(
                [sys.executable, "-m", "yt_dlp",
                 "--cookies-from-browser", browser,
                 "--cookies", _COOKIE_FILE,
                 "--skip-download", "--quiet",
                 "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
                capture_output=True, timeout=30
            )
            if os.path.exists(_COOKIE_FILE) and os.path.getsize(_COOKIE_FILE) > 100:
                print(f"  ✓ Cookies from {browser} ready")
                return
        except Exception as e:
            print(f"  {browser}: {e}")
    print("  ⚠ No cookies — some tracks may fail")

def _ydl_base_opts():
    """Base yt-dlp options shared across all calls."""
    opts = {
        "quiet": False,
        "no_warnings": True,
        "no_part": True,
        "retries": 3,
    }
    if os.path.exists(_COOKIE_FILE) and os.path.getsize(_COOKIE_FILE) > 100:
        opts["cookiefile"] = _COOKIE_FILE
    else:
        opts["cookiesfrombrowser"] = ("chrome",)
    return opts


# ── Downloader ────────────────────────────────────────────────────────────────

def _do_download(video_id, meta):
    """
    Download audio to the library folder. Runs in a background thread.
    Uses Chrome cookies directly (Always Allow granted) — no file export needed.
    Keeps it simple: let yt-dlp pick the best available format.
    """
    out_template = str(LIBRARY_DIR / f"{video_id}.%(ext)s")

    with download_lock:
        download_status[video_id] = {"state": "downloading", "msg": "Downloading…"}

    try:
        print(f"  Downloading {video_id} via yt-dlp…")
        dl_error = None
        info = None

        # Try multiple player clients — tv_embedded bypasses PO token requirement
        client_attempts = [
            ("tv_embedded",  ["tv_embedded"]),
            ("android",      ["android"]),
            ("web_creator",  ["web_creator"]),
            ("web",          ["web"]),
        ]

        base_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "quiet": False,
            "no_warnings": True,
            "no_part": True,
        }
        if os.path.exists(_COOKIE_FILE) and os.path.getsize(_COOKIE_FILE) > 100:
            base_opts["cookiefile"] = _COOKIE_FILE
        else:
            base_opts["cookiesfrombrowser"] = ("chrome",)

        for client_name, client_list in client_attempts:
            ydl_opts = {
                **base_opts,
                "extractor_args": {
                    "youtube": {"player_client": client_list}
                },
            }
            try:
                print(f"  yt-dlp attempt: {client_name} client…")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(
                        f"https://www.youtube.com/watch?v={video_id}", download=True
                    )
                if find_track_file(video_id):
                    print(f"  ✓ yt-dlp succeeded with {client_name}")
                    dl_error = None
                    break
            except Exception as e:
                dl_error = e
                print(f"  {client_name} failed: {e}")

        if dl_error or not find_track_file(video_id):
            print(f"  All yt-dlp clients failed, trying pytubefix…")

        # If yt-dlp failed, try pytubefix (different auth mechanism)
        if dl_error or not find_track_file(video_id):
            try:
                from pytubefix import YouTube
                print(f"  pytubefix: fetching {video_id} (token: {PYTUBEFIX_TOKEN_FILE})…")
                yt = YouTube(
                    f"https://www.youtube.com/watch?v={video_id}",
                    use_oauth=True,
                    allow_oauth_cache=True,
                    token_file=PYTUBEFIX_TOKEN_FILE,
                )
                stream = (
                    yt.streams.filter(only_audio=True, file_extension="mp4").first()
                    or yt.streams.filter(only_audio=True).first()
                    or yt.streams.first()
                )
                if not stream:
                    raise Exception("No streams available")
                out_path = stream.download(
                    output_path=str(LIBRARY_DIR),
                    filename=f"{video_id}.mp4"
                )
                print(f"  pytubefix downloaded: {out_path}")
                info = {"title": yt.title, "uploader": yt.author}
                dl_error = None  # success
            except Exception as e2:
                if dl_error:
                    raise Exception(f"yt-dlp: {dl_error} | pytubefix: {e2}")
                raise e2

        # Find the downloaded file
        track_file = find_track_file(video_id)
        if not track_file:
            for f in LIBRARY_DIR.iterdir():
                if f.stem == video_id and f.suffix not in (".json", ".txt", ".part"):
                    track_file = f
                    break

        if not track_file or track_file.stat().st_size == 0:
            raise Exception("File not found after download")

        size_kb = track_file.stat().st_size // 1024
        print(f"  ✓ Downloaded: {track_file.name} ({size_kb} KB)")

        index = load_index()
        index[video_id] = {
            "id":        video_id,
            "title":     meta.get("title") or info.get("title", "Unknown"),
            "artist":    meta.get("artist") or info.get("uploader", ""),
            "duration":  meta.get("duration", ""),
            "thumbnail": meta.get("thumbnail") or f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
            "file":      track_file.name,
            "added":     int(time.time()),
        }
        save_index(index)

        with download_lock:
            download_status[video_id] = {"state": "done", "msg": "Ready"}

    except Exception as e:
        print(f"  ✗ Download failed: {e}")
        with download_lock:
            download_status[video_id] = {"state": "error", "msg": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Skins API ──────────────────────────────────────────────────────────────────

@app.route("/api/skins")
def list_skins():
    """Return metadata for all installed skins."""
    if not SKINS_DIR.exists():
        return jsonify({"skins": []})
    skins = []
    for skin_dir in sorted(SKINS_DIR.iterdir()):
        meta_file = skin_dir / "skin.json"
        if not skin_dir.is_dir() or not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            meta["hasPreview"] = (skin_dir / "preview.svg").exists()
            meta["hasCss"]     = (skin_dir / "theme.css").exists()
            skins.append(meta)
        except Exception:
            continue
    return jsonify({"skins": skins})


@app.route("/skins/<skin_id>/<filename>")
def serve_skin_file(skin_id, filename):
    """Serve a skin asset (theme.css, preview.svg, skin.json)."""
    allowed = {"theme.css", "preview.svg", "skin.json"}
    if filename not in allowed:
        return "Not found", 404
    skin_dir = SKINS_DIR / skin_id
    f = skin_dir / filename
    if not f.exists():
        return "Not found", 404
    mime = {"theme.css": "text/css", "preview.svg": "image/svg+xml", "skin.json": "application/json"}
    return send_file(str(f), mimetype=mime[filename])


@app.route("/assets/<filename>")
def serve_asset(filename):
    """Serve logo and other static assets from the assets-logos folder."""
    f = ASSETS_DIR / filename
    if not f.exists():
        return "Not found", 404
    return send_file(str(f))


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})

    ydl_opts = {
        **_ydl_base_opts(),
        "quiet": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch15",
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        raw = ydl.extract_info(f"ytsearch15:{query}", download=False)

    index = load_index()
    results = []
    for entry in (raw.get("entries") or []):
        if not entry or not entry.get("id"):
            continue
        vid_id = entry["id"]
        dur = entry.get("duration") or 0
        m, s = divmod(int(dur), 60)
        results.append({
            "id":        vid_id,
            "title":     entry.get("title", "Unknown"),
            "artist":    entry.get("uploader") or entry.get("channel") or "",
            "duration":  f"{m}:{s:02d}" if dur else "",
            "thumbnail": entry.get("thumbnail") or f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
            "inLibrary": vid_id in index,
        })

    return jsonify({"results": results})


@app.route("/api/library")
def library():
    index = load_index()
    tracks = sorted(index.values(), key=lambda t: t.get("added", 0), reverse=True)
    # Annotate with download status
    for t in tracks:
        status = download_status.get(t["id"], {})
        t["downloadState"] = status.get("state", "done")
    return jsonify({"tracks": tracks})


@app.route("/api/download", methods=["POST"])
def download():
    """Queue a track for background download to the library."""
    data = request.json or {}
    video_id = data.get("video_id") or data.get("id")   # frontend sends "id"
    if not video_id:
        return jsonify({"success": False, "error": "Missing video_id"})

    # Already in library?
    if find_track_file(video_id):
        return jsonify({"success": True, "state": "done"})

    # Already downloading?
    status = download_status.get(video_id, {})
    if status.get("state") == "downloading":
        return jsonify({"success": True, "state": "downloading"})

    # Kick off background download
    meta = {k: data.get(k, "") for k in ("title", "artist", "duration", "thumbnail")}
    t = threading.Thread(target=_do_download, args=(video_id, meta), daemon=True)
    t.start()
    return jsonify({"success": True, "state": "downloading"})


@app.route("/api/download-status/<video_id>")
def download_status_route(video_id):
    status = download_status.get(video_id, {})
    in_lib = find_track_file(video_id) is not None
    return jsonify({
        "state": "done" if in_lib else status.get("state", "idle"),
        "msg":   status.get("msg", ""),
    })


@app.route("/api/delete", methods=["POST"])
def delete_track():
    video_id = (request.json or {}).get("video_id")
    if not video_id:
        return jsonify({"success": False})
    # Remove file
    f = find_track_file(video_id)
    if f:
        f.unlink(missing_ok=True)
    # Remove from index
    index = load_index()
    index.pop(video_id, None)
    save_index(index)
    download_status.pop(video_id, None)
    return jsonify({"success": True})


@app.route("/api/devices")
def devices():
    try:
        zones = list(soco.discover(timeout=5) or [])
        return jsonify({"devices": [
            {"name": z.player_name, "ip": z.ip_address} for z in zones
        ]})
    except Exception as e:
        return jsonify({"devices": [], "error": str(e)})


@app.route("/api/rooms")
def rooms():
    """Return all rooms with name, IP, volume, group info, and playback state."""
    try:
        zones = list(soco.discover(timeout=5) or [])
        result = []
        for z in zones:
            try:
                transport = z.get_current_transport_info()
                state = transport.get("current_transport_state", "STOPPED")
            except Exception:
                state = "STOPPED"
            group_members = []
            group_coord_ip = None
            try:
                if z.group:
                    group_members = [m.player_name for m in z.group.members if m.ip_address != z.ip_address]
                    group_coord_ip = z.group.coordinator.ip_address
            except Exception:
                pass
            result.append({
                "name":     z.player_name,
                "ip":       z.ip_address,
                "volume":   z.volume,
                "state":    state,
                "isCoord":  (z.group.coordinator.ip_address == z.ip_address) if z.group else True,
                "group":    group_members,
                "group_id": group_coord_ip,  # coordinator IP — all group members share this value
            })
        result.sort(key=lambda r: r["name"])
        return jsonify({"rooms": result})
    except Exception as e:
        return jsonify({"rooms": [], "error": str(e)})


@app.route("/api/rooms/volume", methods=["POST"])
def room_volume():
    """Set volume for a specific room."""
    data = request.json or {}
    ip   = data.get("ip")
    vol  = data.get("volume", 50)
    if not ip:
        return jsonify({"success": False, "error": "Missing ip"})
    try:
        zone = soco.SoCo(ip)
        zone.volume = max(0, min(100, int(vol)))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/rooms/group", methods=["POST"])
def room_group():
    """Join or unjoin a room from a group."""
    data   = request.json or {}
    ip     = data.get("ip")
    action = data.get("action")   # "join" | "unjoin"
    coord  = data.get("coordinator_ip")
    if not ip:
        return jsonify({"success": False, "error": "Missing ip"})
    try:
        zone = soco.SoCo(ip)
        if action == "unjoin":
            zone.unjoin()
        elif action == "join" and coord:
            coordinator = soco.SoCo(coord)
            zone.join(coordinator)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/audio/<video_id>")
def audio(video_id):
    """Serve local audio file to Sonos. Flask handles Range/seek automatically."""
    f = find_track_file(video_id)
    if not f:
        return "Not in library", 404
    ext = f.suffix.lstrip(".")
    mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mp3": "audio/mpeg",
                "webm": "audio/webm", "ogg": "audio/ogg"}
    mime = mime_map.get(ext, "audio/mp4")
    print(f"  → Serving {f.name} ({f.stat().st_size//1024}KB) as {mime}")
    return send_file(str(f), mimetype=mime, conditional=True)


# Stream URL cache: video_id -> {"url": str, "mime": str, "expires": float}
_stream_cache = {}
_stream_cache_lock = threading.Lock()

def _get_stream_url(video_id):
    """
    Extract a direct YouTube audio stream URL via yt-dlp (no download).
    Caches result for 5 hours (YouTube URLs expire after ~6h).
    Uses the exported cookie file — cookiesfrombrowser can fail inside Flask on macOS
    because it can't access the Keychain outside of a Terminal session.
    """
    now = time.time()
    with _stream_cache_lock:
        cached = _stream_cache.get(video_id)
        if cached and cached["expires"] > now:
            return cached["url"], cached["mime"]

    base = {
        # Sonos only plays AAC/MP3 — WebM/Opus gets rejected with UPnP error 714.
        # Preference order: m4a (AAC in MP4 container) → mp4 audio → mp3 → anything
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio[ext=mp4]/bestaudio[ext=mp3]/bestaudio",
        "quiet": True,
        "no_warnings": True,
    }
    # Prefer the pre-exported cookie file; fall back to live browser read
    if os.path.exists(_COOKIE_FILE) and os.path.getsize(_COOKIE_FILE) > 100:
        base["cookiefile"] = _COOKIE_FILE
    else:
        base["cookiesfrombrowser"] = ("chrome",)

    # Try clients in order — tv_embedded bypasses PO token requirement
    last_err = None
    info = None
    for clients in (["tv_embedded"], ["android"], ["web_creator"], ["web"]):
        opts = {**base, "extractor_args": {"youtube": {"player_client": clients}}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=False
                )
            if info:
                break
        except Exception as e:
            last_err = e
            continue

    if not info:
        raise Exception(last_err or "Could not extract stream URL")

    # Pick the best Sonos-compatible audio format (AAC > MP3 > anything non-webm)
    # Sonos rejects WebM/Opus with UPnP error 714
    SONOS_OK = {"m4a", "mp4", "mp3", "aac", "mp4a"}
    fmt = None
    for f in (info.get("formats") or []):
        if f.get("acodec") == "none" or f.get("vcodec") not in (None, "none"):
            continue
        ext_f = (f.get("ext") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        sonos_ok = ext_f in SONOS_OK or acodec.startswith("mp4a") or ext_f == "mp3"
        if not sonos_ok:
            continue
        if fmt is None or (f.get("abr") or 0) > (fmt.get("abr") or 0):
            fmt = f
    if not fmt:
        # fallback — use whatever yt-dlp selected (may or may not work)
        fmt = info

    url = fmt.get("url") or info.get("url")
    ext = (fmt.get("ext") or "").lower()
    acodec = (fmt.get("acodec") or "").lower()
    if ext == "mp3" or acodec == "mp3":
        mime = "audio/mpeg"
    elif ext in ("webm", "ogg") or acodec.startswith("opus") or acodec.startswith("vorbis"):
        # Last resort — try webm but log it; Sonos may reject
        mime = "audio/webm"
        print(f"  ⚠ stream/{video_id}: WebM/Opus format — Sonos may reject (UPnP 714)")
    else:
        mime = "audio/mp4"  # m4a / mp4 / aac — Sonos handles fine
    print(f"  stream/{video_id}: ext={ext} acodec={acodec} mime={mime}")

    with _stream_cache_lock:
        _stream_cache[video_id] = {"url": url, "mime": mime, "expires": now + 5 * 3600}

    return url, mime


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    """
    Proxy a YouTube audio stream to Sonos without downloading.
    Resolves the direct URL via yt-dlp, then pipes it through Flask.
    Sonos just sees a normal HTTP audio endpoint.
    """
    import requests as req_lib

    try:
        stream_url, mime = _get_stream_url(video_id)
    except Exception as e:
        print(f"  ✗ stream URL error for {video_id}: {e}")
        return f"Stream error: {e}", 500

    from flask import Response, stream_with_context

    range_header = request.headers.get("Range", "bytes=0-")
    headers = {
        "Range": range_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.youtube.com/",
        "Origin": "https://www.youtube.com",
    }

    upstream = req_lib.get(stream_url, headers=headers, stream=True, timeout=15)
    status   = upstream.status_code  # 206 Partial or 200

    resp_headers = {
        "Content-Type":  mime,
        "Accept-Ranges": "bytes",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    print(f"  → Streaming {video_id} ({mime}) range={range_header} status={status}")

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()), status=status,
                    headers=resp_headers, mimetype=mime)


@app.route("/api/stream-info/<video_id>")
def stream_info(video_id):
    """Pre-resolve a stream URL so playback starts instantly. Call before /api/play."""
    try:
        url, mime = _get_stream_url(video_id)
        return jsonify({"ok": True, "mime": mime})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/play", methods=["POST"])
def play():
    data      = request.json or {}
    device_ip = data.get("device_ip")
    video_id  = data.get("video_id") or data.get("id")   # frontend sends "id"
    title     = data.get("title", "")
    artist    = data.get("artist", "")

    if not device_ip or not video_id:
        return jsonify({"success": False, "error": "Missing params"})

    # Spotify tracks: resolve to a YouTube video ID on the fly
    if str(video_id).startswith("sp_"):
        spotify_id = video_id[3:]
        yt_id = _sp_resolve_yt(spotify_id, title, artist)
        if not yt_id:
            return jsonify({"success": False, "error": "Could not find track on YouTube"})
        video_id = yt_id

    # Last.fm tracks: resolve to a YouTube video ID on the fly
    if str(video_id).startswith("lfm_"):
        yt_id = _lfm_resolve_yt(video_id, title, artist)
        if not yt_id:
            return jsonify({"success": False, "error": "Could not find track on YouTube"})
        video_id = yt_id

    track_file = find_track_file(video_id)

    if track_file:
        # Local file — serve directly
        ext = track_file.suffix.lstrip(".")
        mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mp3": "audio/mpeg"}
        mime = mime_map.get(ext, "audio/mp4")
        stream_url = f"http://{LOCAL_IP}:{PORT}/audio/{video_id}"
        print(f"  → play (local): {stream_url}")
    else:
        # No local file — stream live from YouTube via proxy
        print(f"  → play (stream): resolving {video_id}…")
        try:
            _, mime = _get_stream_url(video_id)
        except Exception as e:
            return jsonify({"success": False, "error": f"Stream error: {e}"})
        stream_url = f"http://{LOCAL_IP}:{PORT}/stream/{video_id}"
        print(f"  → play (stream): {stream_url}")

    safe_title  = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    safe_artist = artist.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f'<dc:title>{safe_title}</dc:title>'
        f'<dc:creator>{safe_artist}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{mime}:*">{stream_url}</res>'
        '</item>'
        '</DIDL-Lite>'
    )

    try:
        zone = soco.SoCo(device_ip)
        coordinator = zone.group.coordinator if zone.group else zone
        print(f"  → play_uri [{coordinator.player_name}]: {stream_url}")
        coordinator.play_uri(stream_url, meta=didl, title=title)
        # Update queue index if this track is in the queue
        global play_queue_idx
        with queue_lock:
            for i, t in enumerate(play_queue):
                if t.get("id") == video_id:
                    play_queue_idx = i
                    break
        save_queue_state()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/control", methods=["POST"])
def control():
    data      = request.json or {}
    device_ip = data.get("device_ip")
    action    = data.get("action")
    if not device_ip:
        return jsonify({"success": False, "error": "Missing device_ip"})
    try:
        zone = soco.SoCo(device_ip)
        coordinator = zone.group.coordinator if zone.group else zone
        if action == "play":   coordinator.play()
        elif action == "pause": coordinator.pause()
        elif action == "volume":
            # Single speaker (or group fallback) — set directly
            zone.volume = max(0, min(100, int(data.get("value", 50))))
        elif action == "volume_group":
            # Proportional group volume — frontend has already computed the
            # per-speaker target volumes; we just apply them in parallel.
            from concurrent.futures import ThreadPoolExecutor
            targets = data.get("targets", [])   # [{ip, volume}, ...]
            def set_one(t):
                try:
                    soco.SoCo(t["ip"]).volume = max(0, min(100, int(t["volume"])))
                except Exception:
                    pass
            if targets:
                with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                    list(ex.map(set_one, targets))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/status")
def status():
    device_ip = request.args.get("device_ip")
    if not device_ip:
        return jsonify({})
    try:
        zone      = soco.SoCo(device_ip)
        track     = zone.get_current_track_info()
        transport = zone.get_current_transport_info()

        # Try to get album art — Sonos returns a URI, may be relative
        thumbnail = ""
        art_uri = track.get("album_art", "") or ""
        if art_uri:
            if art_uri.startswith("http"):
                thumbnail = art_uri
            elif art_uri.startswith("/"):
                # Relative URI served by the Sonos speaker itself
                thumbnail = f"http://{device_ip}:1400{art_uri}"

        # Extract YouTube video ID from URI if Sonosphere served this track
        uri = track.get("uri", "")
        uri_id = ""
        if "/audio/" in uri:
            uri_id = uri.split("/audio/")[-1].split("?")[0]
        elif "/stream/" in uri:
            uri_id = uri.split("/stream/")[-1].split("?")[0]

        # Report group-average volume — read all members in parallel
        if zone.group and len(zone.group.members) > 1:
            from concurrent.futures import ThreadPoolExecutor
            members = list(zone.group.members)
            def get_vol(m):
                try: return m.volume
                except Exception: return None
            with ThreadPoolExecutor(max_workers=len(members)) as ex:
                vols = [v for v in ex.map(get_vol, members) if v is not None]
            volume = round(sum(vols) / len(vols)) if vols else zone.volume
        else:
            volume = zone.volume

        return jsonify({
            "state":     transport.get("current_transport_state", "STOPPED"),
            "title":     track.get("title", ""),
            "artist":    track.get("artist", ""),
            "position":  track.get("position", "0:00:00"),
            "duration":  track.get("duration", "0:00:00"),
            "volume":    volume,
            "thumbnail": thumbnail,
            "uri_id":    uri_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── ytmusicapi helpers ────────────────────────────────────────────────────────
# Auth file: browser request headers pasted from Chrome DevTools (ytmusicapi.setup())
YTM_AUTH_FILE = str(LIBRARY_DIR / "ytm_headers.json")

def _parse_cookies_from_file():
    """Parse Netscape cookie file → dict of YouTube cookies."""
    cookies = {}
    if not os.path.exists(_COOKIE_FILE) or os.path.getsize(_COOKIE_FILE) < 100:
        return cookies
    with open(_COOKIE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                continue
            domain = parts[0]
            name, value = parts[5], parts[6]
            if 'youtube' in domain:
                cookies[name] = value
    return cookies


def _compute_sapisidhash(sapisid):
    """Compute the SAPISIDHASH authorization token ytmusicapi needs."""
    import hashlib, time as _time
    ts = int(_time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} https://music.youtube.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _build_ytm_client_from_cookies():
    """
    Build a working YTMusic client from Chrome cookies.
    Uses ytmusicapi.setup(headers_raw=...) to generate a properly-formatted
    auth file, then loads it the normal way — no internal hacking needed.
    """
    try:
        import ytmusicapi
        from ytmusicapi import YTMusic

        cookies = _parse_cookies_from_file()
        if not cookies:
            print("  ⚠ No Chrome cookies found — open Chrome and sign into YouTube, then restart")
            return None

        sapisid = cookies.get('__Secure-3PAPISID') or cookies.get('SAPISID')
        if not sapisid:
            print("  ⚠ Not signed into YouTube in Chrome — sign in and restart")
            return None

        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        sapisidhash = _compute_sapisidhash(sapisid)

        # Build a raw headers string in the format ytmusicapi.setup() expects
        raw_headers = "\n".join([
            "accept: */*",
            "accept-encoding: gzip, deflate",
            "accept-language: en-US,en;q=0.9",
            f"authorization: {sapisidhash}",
            "content-type: application/json",
            f"cookie: {cookie_str}",
            "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-goog-authuser: 0",
            "x-origin: https://music.youtube.com",
        ])

        # Let ytmusicapi parse and format the auth file itself — proper format guaranteed
        ytmusicapi.setup(filepath=YTM_AUTH_FILE, headers_raw=raw_headers)
        ytm = YTMusic(auth=YTM_AUTH_FILE)
        print("  ✓ YouTube Music connected via Chrome cookies")
        return ytm

    except Exception as e:
        print(f"  ⚠ YouTube Music cookie auth failed: {e}")
        return None


def _get_ytm():
    """Return a cached YTMusic client, built automatically from Chrome cookies."""
    global _ytm_client
    with _ytm_lock:
        if _ytm_client is not None:
            return _ytm_client
        _ytm_client = _build_ytm_client_from_cookies()
        return _ytm_client


def _init_ytm_oauth():
    """Auto-connect YouTube Music at startup — no user interaction."""
    # Delete any stale auth files from old approaches so they don't cause confusion
    for stale in (YTM_AUTH_FILE, str(LIBRARY_DIR / "ytm_oauth.json")):
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except Exception:
                pass
    print("  Connecting YouTube Music from Chrome cookies…")
    client = _build_ytm_client_from_cookies()
    if client:
        global _ytm_client
        _ytm_client = client
    else:
        print("  ⚠ YouTube Music unavailable (playlists/liked songs won't show)")


def _fmt_track(item):
    """Normalise a ytmusicapi track dict into our standard format."""
    vid = None
    if isinstance(item.get("videoId"), str):
        vid = item["videoId"]
    elif isinstance(item.get("videoDetails"), dict):
        vid = item["videoDetails"].get("videoId")
    if not vid:
        return None

    title  = item.get("title") or "Unknown"
    artist = ""
    for key in ("artists", "artist"):
        a = item.get(key)
        if isinstance(a, list) and a:
            artist = a[0].get("name", "")
            break
        if isinstance(a, str):
            artist = a
            break

    thumb = ""
    tlist = item.get("thumbnails") or []
    if tlist:
        thumb = tlist[-1].get("url", "")
    if not thumb:
        thumb = f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"

    dur = item.get("duration") or item.get("length") or ""

    return {
        "id":        vid,
        "title":     title,
        "artist":    artist,
        "duration":  str(dur) if dur else "",
        "thumbnail": thumb,
        "inLibrary": find_track_file(vid) is not None,
    }


def _ydl_cookie_opts():
    """Base yt-dlp options with cookie auth.
    Always reads directly from Chrome's live cookie store for authenticated routes —
    the exported cookie file misses httpOnly cookies that YouTube needs for the
    SAPISIDHASH Authorization header.
    """
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    opts["cookiesfrombrowser"] = ("chrome",)
    return opts


@app.route("/api/ytm/playlists")
def ytm_playlists():
    """Fetch user's YouTube playlists via yt-dlp — works with Chrome cookies, no API key needed."""
    try:
        opts = _ydl_cookie_opts()
        opts.update({
            "ignore_no_formats_error": True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/feed/playlists",
                download=False
            )
        entries = info.get("entries", []) if info else []
        playlists = []
        for p in entries:
            pid = p.get("id") or p.get("playlist_id") or ""
            if not pid:
                continue
            playlists.append({
                "id":        pid,
                "title":     p.get("title") or p.get("playlist_title") or "Untitled",
                "count":     p.get("playlist_count") or "",
                "thumbnail": p.get("thumbnail") or p.get("thumbnails", [{}])[-1].get("url", "") if isinstance(p.get("thumbnails"), list) else "",
            })
        return jsonify({"playlists": playlists})
    except Exception as e:
        return jsonify({"error": str(e), "playlists": []})


@app.route("/api/ytm/playlist/<playlist_id>")
def ytm_playlist_tracks(playlist_id):
    """Fetch tracks from a YouTube playlist via yt-dlp."""
    try:
        opts = _ydl_cookie_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}",
                download=False
            )
        entries = info.get("entries", []) if info else []
        idx = load_index()
        tracks = []
        for e in entries:
            vid = e.get("id") or e.get("video_id")
            if not vid:
                continue
            thumb = e.get("thumbnail") or f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"
            dur = e.get("duration")
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
            tracks.append({
                "id":        vid,
                "title":     e.get("title") or "Unknown",
                "artist":    e.get("uploader") or e.get("channel") or "",
                "duration":  dur_str,
                "thumbnail": thumb,
                "inLibrary": vid in idx,
            })
        return jsonify({"title": info.get("title", "Playlist") if info else "Playlist", "tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})


@app.route("/api/ytm/liked")
def ytm_liked():
    """Fetch liked videos via yt-dlp."""
    try:
        opts = _ydl_cookie_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/playlist?list=LL",
                download=False
            )
        entries = info.get("entries", []) if info else []
        idx = load_index()
        tracks = []
        for e in entries:
            vid = e.get("id") or e.get("video_id")
            if not vid:
                continue
            thumb = e.get("thumbnail") or f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"
            dur = e.get("duration")
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
            tracks.append({
                "id":        vid,
                "title":     e.get("title") or "Unknown",
                "artist":    e.get("uploader") or e.get("channel") or "",
                "duration":  dur_str,
                "thumbnail": thumb,
                "inLibrary": vid in idx,
            })
        return jsonify({"tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})



# ── Last.fm helpers + routes ──────────────────────────────────────────────────

def _lfm(method, **params):
    """Call the Last.fm API and return parsed JSON."""
    import urllib.request, urllib.parse
    qs = urllib.parse.urlencode({
        "method": method, "api_key": LASTFM_API_KEY, "format": "json", **params
    })
    req = urllib.request.Request(
        f"https://ws.audioscrobbler.com/2.0/?{qs}",
        headers={"User-Agent": "Sonosphere/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _lfm_track(t, artist_override=""):
    """Normalise a Last.fm track dict to Sonosphere's standard format."""
    artist = (t.get("artist") or {})
    artist_name = artist.get("name","") if isinstance(artist,dict) else str(artist)
    artist_name = artist_name or artist_override
    title  = t.get("name","Unknown")
    # Build a stable id from artist+title
    safe   = lambda s: s.lower().replace(" ","_")[:40]
    tid    = f"lfm_{safe(artist_name)}_{safe(title)}"
    # Last.fm doesn't give durations in chart/tag endpoints — leave blank
    dur_s  = t.get("duration","0")
    try:
        dur_s = int(dur_s)
        dur_str = f"{dur_s//60}:{dur_s%60:02d}" if dur_s else ""
    except Exception:
        dur_str = ""
    # Thumbnail: use Last.fm image or fall back to a YouTube search thumb later
    images = t.get("image",[])
    thumb  = ""
    for img in reversed(images):
        if img.get("#text"):
            thumb = img["#text"]; break
    return {
        "id":       tid,
        "lfm_artist": artist_name,
        "lfm_title":  title,
        "title":    title,
        "artist":   artist_name,
        "duration": dur_str,
        "thumbnail": thumb,
        "source":   "lastfm",
    }

_lfm_yt_cache = {}   # lfm_xxx → youtube_video_id

def _lfm_resolve_yt(track_id, title, artist):
    """Resolve a Last.fm track to a YouTube video ID (cached)."""
    if track_id in _lfm_yt_cache:
        return _lfm_yt_cache[track_id]
    query = f"ytsearch1:{title} {artist} audio"
    ydl_opts = {**_ydl_base_opts(), "quiet": True, "extract_flat": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(query, download=False)
        entries = raw.get("entries",[]) if raw else []
        yt_id   = entries[0]["id"] if entries else None
        if yt_id:
            _lfm_yt_cache[track_id] = yt_id
        return yt_id
    except Exception:
        return None

@app.route("/api/discover/charts")
def discover_charts():
    try:
        data   = _lfm("chart.getTopTracks", limit=25)
        tracks = [_lfm_track(t) for t in data.get("tracks",{}).get("track",[]) if t]
        return jsonify({"tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})

@app.route("/api/discover/genre/<tag>")
def discover_genre(tag):
    try:
        data   = _lfm("tag.getTopTracks", tag=tag, limit=30)
        tracks = [_lfm_track(t) for t in data.get("tracks",{}).get("track",[]) if t]
        return jsonify({"tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})

@app.route("/api/discover/artist/<name>")
def discover_artist(name):
    try:
        top_data  = _lfm("artist.getTopTracks", artist=name, limit=20)
        sim_data  = _lfm("artist.getSimilar",   artist=name, limit=10)
        tracks    = [_lfm_track(t, name) for t in top_data.get("toptracks",{}).get("track",[]) if t]
        similar   = [a.get("name","") for a in sim_data.get("similarartists",{}).get("artist",[]) if a.get("name")]
        # Canonical artist name from top tracks result
        canon = top_data.get("toptracks",{}).get("@attr",{}).get("artist","") or name
        return jsonify({"artist": canon, "tracks": tracks, "similar": similar})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": [], "similar": []})


# ── Spotify helpers ───────────────────────────────────────────────────────────

def _sp_load_tokens():
    try:
        if SPOTIFY_TOKEN_FILE and SPOTIFY_TOKEN_FILE.exists():
            return json.loads(SPOTIFY_TOKEN_FILE.read_text())
    except Exception:
        pass
    return None

def _sp_save_tokens(tokens):
    try:
        SPOTIFY_TOKEN_FILE.write_text(json.dumps(tokens))
    except Exception:
        pass

def _sp_refresh(tokens):
    import urllib.request, urllib.parse, base64
    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    data  = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req) as r:
        new = json.loads(r.read())
    tokens["access_token"] = new["access_token"]
    if "refresh_token" in new:
        tokens["refresh_token"] = new["refresh_token"]
    tokens["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
    _sp_save_tokens(tokens)
    return tokens

def _sp_token():
    """Return a valid Spotify access token, refreshing if needed."""
    tokens = _sp_load_tokens()
    if not tokens:
        return None
    if time.time() > tokens.get("expires_at", 0):
        try:
            tokens = _sp_refresh(tokens)
        except Exception:
            return None
    return tokens.get("access_token")

def _sp_get(path, token=None):
    """Call the Spotify Web API and return parsed JSON."""
    import urllib.request
    if token is None:
        token = _sp_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def _sp_track(item):
    """Normalise a Spotify track item to Sonosphere's standard track dict."""
    t = item.get("track") or item   # playlist items wrap in {"track": ...}
    if not t or not t.get("id"):
        return None
    artists  = ", ".join(a["name"] for a in t.get("artists", []))
    dur_ms   = t.get("duration_ms", 0)
    dur_str  = f"{dur_ms//60000}:{(dur_ms//1000)%60:02d}"
    images   = t.get("album", {}).get("images", [])
    thumb    = images[0]["url"] if images else ""
    return {
        "id":         f"sp_{t['id']}",
        "spotify_id": t["id"],
        "title":      t.get("name", "Unknown"),
        "artist":     artists,
        "duration":   dur_str,
        "thumbnail":  thumb,
        "source":     "spotify",
    }

def _sp_resolve_yt(spotify_id, title, artist):
    """Find the YouTube video ID for a Spotify track (cached)."""
    cache_key = f"sp_{spotify_id}"
    if cache_key in _spotify_yt_cache:
        return _spotify_yt_cache[cache_key]
    query = f"ytsearch1:{title} {artist} audio"
    ydl_opts = {**_ydl_base_opts(), "quiet": True, "extract_flat": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(query, download=False)
        entries = raw.get("entries", []) if raw else []
        yt_id   = entries[0]["id"] if entries else None
        if yt_id:
            _spotify_yt_cache[cache_key] = yt_id
        return yt_id
    except Exception:
        return None


# ── Spotify auth routes ────────────────────────────────────────────────────────

@app.route("/auth/spotify")
def auth_spotify():
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id":     SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "scope":         SPOTIFY_SCOPES,
    })
    return redirect(f"https://accounts.spotify.com/authorize?{params}")

@app.route("/auth/spotify/callback")
def auth_spotify_callback():
    import urllib.request, urllib.parse, base64
    code = request.args.get("code")
    if not code:
        return "Spotify auth failed — no code returned", 400
    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    data  = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req) as r:
        tokens = json.loads(r.read())
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600) - 60
    _sp_save_tokens(tokens)
    return redirect("http://127.0.0.1:8888/?spotify=connected")

@app.route("/auth/spotify/status")
def auth_spotify_status():
    return jsonify({"connected": _sp_token() is not None})

@app.route("/auth/spotify/disconnect", methods=["POST"])
def auth_spotify_disconnect():
    try:
        SPOTIFY_TOKEN_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"success": True})


# ── Spotify API routes ─────────────────────────────────────────────────────────

@app.route("/api/spotify/search")
def spotify_search():
    import urllib.parse
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    token = _sp_token()
    if not token:
        return jsonify({"error": "not_connected", "results": []})
    try:
        params  = urllib.parse.urlencode({"q": q, "type": "track", "limit": 20})
        data    = _sp_get(f"/search?{params}", token)
        results = [t for t in (_sp_track(tr) for tr in (data or {}).get("tracks", {}).get("items", [])) if t]
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e), "results": []})

@app.route("/api/spotify/playlists")
def spotify_playlists():
    token = _sp_token()
    if not token:
        return jsonify({"error": "not_connected", "playlists": []})
    try:
        data = _sp_get("/me/playlists?limit=50", token)
        playlists = []
        for p in (data or {}).get("items", []):
            images = p.get("images", [])
            playlists.append({
                "id":        p["id"],
                "title":     p.get("name", "Untitled"),
                "count":     p.get("tracks", {}).get("total", ""),
                "thumbnail": images[0]["url"] if images else "",
            })
        return jsonify({"playlists": playlists})
    except Exception as e:
        return jsonify({"error": str(e), "playlists": []})

@app.route("/api/spotify/playlist/<playlist_id>")
def spotify_playlist_tracks(playlist_id):
    token = _sp_token()
    if not token:
        return jsonify({"error": "not_connected", "tracks": []})
    try:
        data   = _sp_get(f"/playlists/{playlist_id}/tracks?limit=100&fields=items(track(id,name,artists,duration_ms,album(images)))", token)
        tracks = [t for t in (_sp_track(item) for item in (data or {}).get("items", [])) if t]
        meta   = _sp_get(f"/playlists/{playlist_id}?fields=name", token)
        title  = (meta or {}).get("name", "Playlist")
        return jsonify({"title": title, "tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})

@app.route("/api/spotify/liked")
def spotify_liked():
    token = _sp_token()
    if not token:
        return jsonify({"error": "not_connected", "tracks": []})
    try:
        data   = _sp_get("/me/tracks?limit=50", token)
        tracks = [t for t in (_sp_track(item) for item in (data or {}).get("items", [])) if t]
        return jsonify({"tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})


# ── Queue routes ──────────────────────────────────────────────────────────────

@app.route("/api/queue", methods=["GET"])
def get_queue():
    with queue_lock:
        return jsonify({"queue": play_queue, "current": play_queue_idx})


@app.route("/api/queue/add", methods=["POST"])
def queue_add():
    """Add a track to the end of the queue."""
    data  = request.json or {}
    track = {k: data.get(k, "") for k in ("id", "title", "artist", "duration", "thumbnail")}
    if not track["id"]:
        return jsonify({"success": False, "error": "Missing id"})
    with queue_lock:
        play_queue.append(track)
    save_queue_state()
    return jsonify({"success": True, "queue": play_queue, "current": play_queue_idx})


@app.route("/api/queue/next", methods=["POST"])
def queue_next():
    global play_queue_idx
    data      = request.json or {}
    device_ip = data.get("device_ip")
    with queue_lock:
        if not play_queue:
            return jsonify({"success": False, "error": "Queue empty"})
        play_queue_idx = min(play_queue_idx + 1, len(play_queue) - 1)
        track = play_queue[play_queue_idx]
    if device_ip:
        _play_track_on_sonos(track, device_ip)
    save_queue_state()
    return jsonify({"success": True, "track": track, "current": play_queue_idx})


@app.route("/api/queue/prev", methods=["POST"])
def queue_prev():
    global play_queue_idx
    data      = request.json or {}
    device_ip = data.get("device_ip")
    with queue_lock:
        if not play_queue:
            return jsonify({"success": False, "error": "Queue empty"})
        play_queue_idx = max(play_queue_idx - 1, 0)
        track = play_queue[play_queue_idx]
    if device_ip:
        _play_track_on_sonos(track, device_ip)
    save_queue_state()
    return jsonify({"success": True, "track": track, "current": play_queue_idx})


@app.route("/api/queue/jump", methods=["POST"])
def queue_jump():
    global play_queue_idx
    data      = request.json or {}
    device_ip = data.get("device_ip")
    idx       = data.get("index", 0)
    video_id  = data.get("video_id")  # Prefer ID-based lookup to survive index mismatches

    with queue_lock:
        # If a video_id was provided, find the actual position in the current queue.
        # This is the reliable path — index alone can mismatch if a reorder is still
        # in-flight or the backend was restarted with a stale queue_state.json.
        if video_id:
            found = next((i for i, t in enumerate(play_queue) if t.get("id") == video_id), None)
            if found is not None:
                idx = found

        if idx < 0 or idx >= len(play_queue):
            return jsonify({"success": False, "error": "Index out of range"})
        play_queue_idx = idx
        track = play_queue[play_queue_idx]
    if device_ip:
        _play_track_on_sonos(track, device_ip)
    save_queue_state()
    return jsonify({"success": True, "track": track, "current": play_queue_idx})


@app.route("/api/queue/remove", methods=["POST"])
def queue_remove():
    global play_queue_idx
    data = request.json or {}
    idx  = data.get("index", -1)
    with queue_lock:
        if idx < 0 or idx >= len(play_queue):
            return jsonify({"success": False})
        play_queue.pop(idx)
        if play_queue_idx >= idx:
            play_queue_idx = max(play_queue_idx - 1, -1)
    save_queue_state()
    return jsonify({"success": True, "queue": play_queue, "current": play_queue_idx})


@app.route("/api/queue/clear", methods=["POST"])
def queue_clear():
    global play_queue_idx
    with queue_lock:
        play_queue.clear()
        play_queue_idx = -1
    save_queue_state()
    return jsonify({"success": True})


@app.route("/api/queue/reorder", methods=["POST"])
def queue_reorder():
    global play_queue_idx
    data = request.json or {}
    from_idx = data.get("from")
    to_idx   = data.get("to")
    with queue_lock:
        if from_idx is None or to_idx is None:
            return jsonify({"success": False, "error": "Missing from/to"})
        if not (0 <= from_idx < len(play_queue)) or not (0 <= to_idx < len(play_queue)):
            return jsonify({"success": False, "error": "Index out of range"})
        moved = play_queue.pop(from_idx)
        play_queue.insert(to_idx, moved)
        # Keep current index tracking correct
        if play_queue_idx == from_idx:
            play_queue_idx = to_idx
        elif from_idx < play_queue_idx <= to_idx:
            play_queue_idx -= 1
        elif from_idx > play_queue_idx >= to_idx:
            play_queue_idx += 1
    save_queue_state()
    return jsonify({"success": True, "queue": play_queue, "current": play_queue_idx})


@app.route("/api/queue/shuffle", methods=["POST"])
def queue_shuffle():
    global play_queue_idx
    import random
    with queue_lock:
        if not play_queue:
            return jsonify({"success": False})
        # Keep current track at position 0 if playing
        if 0 <= play_queue_idx < len(play_queue):
            current = play_queue.pop(play_queue_idx)
            random.shuffle(play_queue)
            play_queue.insert(0, current)
            play_queue_idx = 0
        else:
            random.shuffle(play_queue)
    save_queue_state()
    return jsonify({"success": True, "queue": play_queue, "current": play_queue_idx})


def _play_track_on_sonos(track, device_ip):
    """Internal: send a track to Sonos. Used by queue nav."""
    global play_queue_idx
    video_id   = track.get("id") or track.get("video_id")
    title      = track.get("title", "")
    artist     = track.get("artist", "")
    # Spotify tracks: resolve to YouTube ID before streaming
    if str(video_id).startswith("sp_"):
        yt_id = _sp_resolve_yt(video_id[3:], title, artist)
        if not yt_id:
            return False, "Could not resolve Spotify track to YouTube"
        video_id = yt_id
    # Last.fm tracks: resolve to YouTube ID before streaming
    if str(video_id).startswith("lfm_"):
        yt_id = _lfm_resolve_yt(video_id, title, artist)
        if not yt_id:
            return False, "Could not resolve Last.fm track to YouTube"
        video_id = yt_id
    track_file = find_track_file(video_id)

    if track_file:
        ext      = track_file.suffix.lstrip(".")
        mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mp3": "audio/mpeg"}
        mime     = mime_map.get(ext, "audio/mp4")
        url      = f"http://{LOCAL_IP}:{PORT}/audio/{video_id}"
    else:
        try:
            _, mime = _get_stream_url(video_id)
        except Exception as e:
            return False, f"Stream error: {e}"
        url = f"http://{LOCAL_IP}:{PORT}/stream/{video_id}"

    safe_title  = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    safe_artist = artist.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f'<dc:title>{safe_title}</dc:title>'
        f'<dc:creator>{safe_artist}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{mime}:*">{url}</res>'
        '</item>'
        '</DIDL-Lite>'
    )
    try:
        zone        = soco.SoCo(device_ip)
        coordinator = zone.group.coordinator if zone.group else zone
        coordinator.play_uri(url, meta=didl, title=title)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _init_pytubefix_oauth():
    """
    Warm up pytubefix with SSL fix for macOS Python installs.
    pytubefix is a fallback downloader — yt-dlp handles most cases.
    """
    try:
        # Fix macOS Python SSL cert issue (Python doesn't use system certs by default)
        import ssl, certifi
        ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass  # certifi not installed — ssl fix skipped, yt-dlp still works fine

    try:
        from pytubefix import YouTube
        yt = YouTube(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            use_oauth=True,
            allow_oauth_cache=True,
            token_file=PYTUBEFIX_TOKEN_FILE,
        )
        _ = yt.title
        print(f"  ✓ pytubefix ready")
    except Exception:
        pass  # pytubefix is fallback only — yt-dlp handles downloads fine without it


if __name__ == "__main__":
    print(f"\n  ♪  Sonosphere")
    print(f"  Library: {LIBRARY_DIR}")
    load_queue_state()
    _init_cookies()
    print()
    _init_pytubefix_oauth()
    print()
    _init_ytm_oauth()
    print(f"\n  ─────────────────────────────────")
    print(f"  LAN IP: {LOCAL_IP}")
    print(f"  Starting…\n")

    # ── Start Flask in a background thread ────────────────────────────────────
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True),
        daemon=True
    )
    flask_thread.start()

    # ── Wait for Flask to be ready ────────────────────────────────────────────
    import urllib.request
    for _ in range(15):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # ── Open Chrome in app mode (no tabs/address bar = standalone window) ────
    import subprocess, webbrowser
    url = f"http://localhost:{PORT}"
    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    launched = False
    for chrome in chrome_paths:
        if os.path.exists(chrome):
            subprocess.Popen([
                chrome,
                f"--app={url}",
                "--window-size=1020,740",
                "--disable-extensions",
            ])
            launched = True
            print(f"  ✓ Opened standalone window via {os.path.basename(os.path.dirname(os.path.dirname(chrome)))}")
            break
    if not launched:
        print("  Chrome not found — opening in default browser…")
        webbrowser.open(url)

    # Keep Flask alive until Ctrl+C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopped.")
