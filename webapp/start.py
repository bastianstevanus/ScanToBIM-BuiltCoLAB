"""
Launcher: starts the FastAPI backend and optionally opens the browser.
Run:  python start.py
Docker: CMD ["python", "start.py"]
"""
import os
import sys
import threading
import time

import uvicorn

PORT = 8000


def _free_port(port: int) -> None:
    """Kill any process already listening on *port* so this instance can bind."""
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return   # port is already free
    except OSError:
        return

    # Port is occupied — try to release it
    try:
        import subprocess, signal
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid and pid != os.getpid():
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                       capture_output=True)
                        time.sleep(0.5)
                        break
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5)
            for pid_str in result.stdout.split():
                pid = int(pid_str.strip())
                if pid and pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.5)
    except Exception:
        pass


def _open_browser() -> None:
    time.sleep(2.5)
    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    _free_port(PORT)

    if not os.environ.get("DOCKER_ENV"):
        threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
