# Headless Sonosphere server for the Mac app's LaunchAgent (com.jimmy.sonosphere).
# Same Flask app as `Start Sonosphere.command`, minus the interactive YouTube sign-in
# prompts and the Chrome window: the Sonosphere.app window connects to it instead.
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
import app  # noqa: E402

app.load_queue_state()
print(f"Sonosphere server on :{app.PORT} (LAN {app.LOCAL_IP})", flush=True)
# 0.0.0.0 because the speakers fetch /audio and /stream from this Mac.
app.app.run(host="0.0.0.0", port=app.PORT, threaded=True)
