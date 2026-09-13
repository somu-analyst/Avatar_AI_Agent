# -*- coding: utf-8 -*-
"""Keeps the Streamlit server running, restarting it whenever it exits.

Needed for the in-app "Restart (picks up code changes)" button: Python
caches krishna.py/avatar_widget.py on import, so a normal browser refresh
does NOT pick up edits to those files -- only app.py's own top-level code
hot-reloads via Streamlit's file watcher. The restart button kills the
current server process (os._exit); this supervisor notices it died and
starts a fresh one, which re-imports everything from disk.

Usage:
    python supervisor.py   # runs forever, Ctrl+C (or kill the process) to stop
"""
import os
import subprocess
import sys
import time

PORT = 8610
ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app.py")
STOP_FLAG = os.path.join(ROOT, ".supervisor_stop")


def run_forever():
    if os.path.exists(STOP_FLAG):
        os.remove(STOP_FLAG)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"   # kokoro's config-file bug workaround
    while True:
        if os.path.exists(STOP_FLAG):
            print("[supervisor] stop flag present, exiting")
            break
        print("[supervisor] starting Streamlit...")
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", APP,
             "--server.port", str(PORT), "--server.address", "127.0.0.1",
             "--server.headless", "true", "--browser.gatherUsageStats", "false"],
            cwd=ROOT, env=env,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        proc.wait()
        print(f"[supervisor] Streamlit exited (code {proc.returncode}) -- restarting in 1s")
        time.sleep(1)


if __name__ == "__main__":
    run_forever()
