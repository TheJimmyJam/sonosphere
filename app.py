import os
import socket
import json
import time
import threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file
import yt_dlp
import soco

app = Flask(__name__)
PORT = 8888

# ── Skins pack (ships alongside app.py in a skins/ folder) ───────────────────
SKINS_DIR = Path(__file__).parent / "skins"

# ── Library folder (permanent, in your Music directory) ───────────────────────
LIBRARY_DIR = Path.home() / "Music" / "SonosPlayer"
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_INDEX = LIBRARY_DIR / ".index.json"         # maps video_id -> track metadata
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
            try:
                if z.group:
                    group_members = [m.player_name for m in z.group.members if m.ip_address != z.ip_address]
            except Exception:
                pass
            result.append({
                "name":     z.player_name,
                "ip":       z.ip_address,
                "volume":   z.volume,
                "state":    state,
                "isCoord":  (z.group.coordinator.ip_address == z.ip_address) if z.group else True,
                "group":    group_members,
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


@app.route("/api/play", methods=["POST"])
def play():
    data      = request.json or {}
    device_ip = data.get("device_ip")
    video_id  = data.get("video_id") or data.get("id")   # frontend sends "id"
    title     = data.get("title", "")
    artist    = data.get("artist", "")

    if not device_ip or not video_id:
        return jsonify({"success": False, "error": "Missing params"})

    track_file = find_track_file(video_id)
    if not track_file:
        return jsonify({"success": False,
                        "error": "Track not in library. Add it first with the ⬇ button."})

    ext = track_file.suffix.lstrip(".")
    mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mp3": "audio/mpeg"}
    mime = mime_map.get(ext, "audio/mp4")
    stream_url = f"http://{LOCAL_IP}:{PORT}/audio/{video_id}"

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
            zone.volume = max(0, min(100, int(data.get("value", 50))))
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
        return jsonify({
            "state":    transport.get("current_transport_state", "STOPPED"),
            "title":    track.get("title", ""),
            "artist":   track.get("artist", ""),
            "position": track.get("position", "0:00:00"),
            "duration": track.get("duration", "0:00:00"),
            "volume":   zone.volume,
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
    Build a working YTMusic client directly from Chrome cookies.
    Injects headers into the session after construction — bypasses all file-based auth.
    Returns a YTMusic instance or None.
    """
    try:
        from ytmusicapi import YTMusic
        cookies = _parse_cookies_from_file()
        if not cookies:
            return None

        sapisid = cookies.get('__Secure-3PAPISID') or cookies.get('SAPISID')
        if not sapisid:
            print("  ⚠ Not signed into YouTube in Chrome — open Chrome and sign in, then restart")
            return None

        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        sapisidhash = _compute_sapisidhash(sapisid)

        injected = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": sapisidhash,
            "content-type": "application/json",
            "cookie": cookie_str,
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-goog-authuser": "0",
            "x-origin": "https://music.youtube.com",
        }

        # Create instance and inject headers + set auth flag so _check_auth() passes
        ytm = YTMusic()
        ytm.headers.update(injected)
        # ytmusicapi checks self.auth before any authenticated call — set it truthy
        ytm.auth = "cookie"
        # Also patch the underlying session if present (ytmusicapi 1.x uses requests.Session)
        if hasattr(ytm, '_session'):
            ytm._session.headers.update(injected)

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


@app.route("/api/ytm/playlists")
def ytm_playlists():
    ytm = _get_ytm()
    if not ytm:
        return jsonify({"error": "YouTube Music not available", "playlists": []})
    try:
        raw = ytm.get_library_playlists(limit=50)
        playlists = []
        for p in raw:
            playlists.append({
                "id":          p.get("playlistId") or p.get("browseId", ""),
                "title":       p.get("title", "Untitled"),
                "count":       p.get("count") or "",
                "thumbnail":   (p.get("thumbnails") or [{}])[-1].get("url", ""),
            })
        return jsonify({"playlists": playlists})
    except Exception as e:
        return jsonify({"error": str(e), "playlists": []})


@app.route("/api/ytm/playlist/<playlist_id>")
def ytm_playlist_tracks(playlist_id):
    ytm = _get_ytm()
    if not ytm:
        return jsonify({"error": "YouTube Music not available", "tracks": []})
    try:
        raw    = ytm.get_playlist(playlist_id, limit=200)
        tracks = [t for t in (_fmt_track(i) for i in raw.get("tracks", [])) if t]
        return jsonify({"title": raw.get("title", "Playlist"), "tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e), "tracks": []})


@app.route("/api/ytm/liked")
def ytm_liked():
    ytm = _get_ytm()
    if not ytm:
        return jsonify({"error": "YouTube Music not available", "tracks": []})
    try:
        raw    = ytm.get_liked_songs(limit=200)
        tracks = [t for t in (_fmt_track(i) for i in raw.get("tracks", [])) if t]
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
    with queue_lock:
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
    track_file = find_track_file(video_id)
    if not track_file:
        return False, "Track not in library"

    ext      = track_file.suffix.lstrip(".")
    mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mp3": "audio/mpeg"}
    mime     = mime_map.get(ext, "audio/mp4")
    url      = f"http://{LOCAL_IP}:{PORT}/audio/{video_id}"

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
