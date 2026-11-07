# Sonosphere: handoff

**Status:** Mac app live on the mini: `~/Applications/Sonosphere.app` + LaunchAgent `com.jimmy.sonosphere` (server :8888). Apple Music tab shipped (main eb10ca8).
**Next:** Jimmy's first real use is the live check of the Apple Music buttons. Optional polish below.

## HANDOFF #1 (2026-11-07 15:40): pick up here
Clocks: My Builds literary-land (log) since 2:25 PM, 1h 08m · none stopped in the last 12 h

1. Install/rebuild: `desktop/install.sh` (venv on ~/.local/bin/python3.11, swiftc app, LaunchAgent). Log `~/Library/Logs/Sonosphere.log`. Push straight to main with the NEW_GITHUB_CLASSIC_PAT (origin URL has no token).
2. Checked: favorites list (37, art), catalog search (albums + songs) and Songs tab in the browser pane; backend play of album / song "play next" / next / station / favorite album, artist radio refused, via Flask test client on the house group (muted, snapshot-restored). App window screenshot shows grouped Den now-playing with ⏮ ⏭ enabled. NOT checked: the Play now / Play next / + Queue buttons clicked in the app.
3. Testing trap: re-check group membership right before any playback test; Office had joined the playing house group. See memory mute-audio-when-testing-playback.
4. DONE 2026-11-07: bottom bar no longer overflows at 1200px (chip row scrolls inside itself; volume block shrinks to 150px min). Measured: 1200px doc width 1200, 1600px chips fully shown.

**Open with Jimmy:** none. FYI given: macOS may ask once to allow Python on the local network; Kitchen/Bathroom speakers report names "Office Rear"/"Office".
**Leftovers:** YouTube sign-in prompts (pytubefix/ytm OAuth, Chrome cookie export) are skipped in the headless server; YouTube search still works without cookies, playlists/liked may not.
**Mode:** auto mode fine.
