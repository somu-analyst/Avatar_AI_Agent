# -*- coding: utf-8 -*-
"""Launch Krishna as a desktop app window -- same pattern as job-search-copilot's
launcher. Starts the Streamlit server if it's not already up, then opens a
dedicated window (pywebview, or Edge/Chrome --app=, or a plain browser tab
as the last resort).

Usage:
    python app_launcher.py
"""
import os
import socket
import subprocess
import sys
import time

PORT = 8610   # distinct from job-search-copilot's 8601 and the NYSE dashboard's 8502
URL = f"http://localhost:{PORT}"
ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app.py")


def _port_open(port=PORT, host="127.0.0.1", timeout=0.6):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def ensure_server(wait=60):
    if _port_open():
        print(f"[krishna] server already up on {PORT}")
        return True
    print("[krishna] starting supervisor (keeps Streamlit alive across restarts)...")
    stop_flag = os.path.join(ROOT, ".supervisor_stop")
    if os.path.exists(stop_flag):
        os.remove(stop_flag)   # a previous clean shutdown left this -- clear it or the
                               # supervisor would see it and refuse to start the server
    subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "supervisor.py")],
        cwd=ROOT,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                       | getattr(subprocess, "DETACHED_PROCESS", 0)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    for _ in range(wait * 2):
        if _port_open():
            print(f"[krishna] server ready on {PORT}")
            return True
        time.sleep(0.5)
    print("[krishna] server did not come up in time")
    return False


def _find(*candidates):
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def open_window():
    try:
        import webview
        print("[krishna] opening native window (pywebview)")
        webview.create_window("Krishna", URL, width=900, height=1000, resizable=True)
        webview.start()
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"[krishna] pywebview failed ({e}), falling back")

    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    edge = _find(os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
                 os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"))
    chrome = _find(os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                   os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                   os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"))
    for exe, name in ((edge, "Edge"), (chrome, "Chrome")):
        if exe:
            print(f"[krishna] opening {name} app window")
            subprocess.Popen([exe, f"--app={URL}", "--window-size=900,1000"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True

    import webbrowser
    print("[krishna] falling back to the default browser")
    return webbrowser.open(URL, new=2)


if __name__ == "__main__":
    if not ensure_server():
        sys.exit(1)
    open_window()
