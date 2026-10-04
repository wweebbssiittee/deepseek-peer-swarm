from __future__ import annotations

import argparse
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from .app import create_app
from .config import data_directory


def main():
    parser = argparse.ArgumentParser(description="Ten equal DeepSeek peers, local dashboard and A2A")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--open", action="store_true", help="Open the local dashboard")
    parser.add_argument("--data-dir", help="Override private state directory")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["SWARM_DATA_DIR"] = args.data_dir
    home = data_directory()
    home.mkdir(parents=True, exist_ok=True)
    lock = open(home / "server.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("This swarm data directory already has a running server.")
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return
    app = create_app(home, port=args.port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="info"))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    if args.open:
        def open_when_ready():
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=1) as result:
                        if result.status == 200:
                            webbrowser.open(f"http://127.0.0.1:{args.port}")
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(0.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
