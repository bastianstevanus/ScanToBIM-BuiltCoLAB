import sys, os, threading, time, webbrowser
import uvicorn

def _open_browser():
    time.sleep(3.0)
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    # Direct import so PyInstaller's analysis detects all backend dependencies.
    from backend.main import app
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
