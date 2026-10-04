"""An isolated local dashboard fixture. Never loads the user's runtime or keys."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

# Running ``python scripts/browser_smoke.py`` must exercise this checkout even
# when the interpreter belongs to a different editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from swarm.app import create_app
from swarm.notifications import Notifier


class FixtureProvider:
    """Deterministic, zero-cost peer activity using coordination tools only."""

    def __init__(self):
        self.seen: set[str] = set()
        self.calls = 0

    async def complete(self, key, config, messages, tools):
        self.calls += 1
        peer = key.removeprefix("offline-fixture-")
        calls = []
        content = None
        if key not in self.seen:
            self.seen.add(key)
            content = f"[OFFLINE FIXTURE] Peer {peer}: ready to share and review work."
            calls.append({"id": f"work-{peer}", "type": "function", "function": {
                "name": "create_work", "arguments": json.dumps({
                    "title": f"Review contribution {peer}",
                    "description": "Deterministic UI fixture; no model, files or commands are used.",
                }),
            }})
        # A settled synthetic response followed by a cancellable tool wait makes
        # pause/resume deterministic without accumulating uncertain API spend.
        calls.append({"id": f"yield-{peer}-{self.calls}", "type": "function", "function": {
            "name": "yield_work", "arguments": '{"seconds":60}',
        }})
        return {"role": "assistant", "content": content, "tool_calls": calls}, {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }

    async def close(self):
        pass


def public_fixture(value, home: Path):
    """Remove machine-specific paths from browser fixture responses/screenshots."""
    if isinstance(value, dict):
        return {key: public_fixture(item, home) for key, item in value.items()}
    if isinstance(value, list):
        return [public_fixture(item, home) for item in value]
    if isinstance(value, str):
        # Paths can appear as strings or embedded serialized event payloads.
        for prefix in (str(home).replace("\\", "\\\\"), str(home), home.as_posix()):
            value = value.replace(prefix, "demo-workspace")
        return value
    return value


@contextmanager
def isolated_server():
    """Serve only fresh temporary state on an ephemeral loopback port."""
    with tempfile.TemporaryDirectory(prefix="swarm-browser-fixture-") as temporary:
        home = Path(temporary).resolve()
        provider = FixtureProvider()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            # Settings reads these variables during construction only. Do not
            # inspect, copy or persist any real environment-provided credentials.
            with patch.dict(os.environ, {f"DEEPSEEK_API_KEY_{i}": "" for i in range(1, 11)}):
                app = create_app(home=home, provider=provider, port=port,
                                 notifier=Notifier(home, play=lambda _: None))
            app.state.settings.keys = [f"offline-fixture-{i:02}" for i in range(1, 11)]
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                  log_level="error", access_log=False))
            failures = []

            def serve():
                try:
                    server.run(sockets=[listener])
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(target=serve, name="offline-browser-fixture", daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started:
                    if failures or not thread.is_alive():
                        raise RuntimeError("Offline fixture failed to start") from (failures[0] if failures else None)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Offline fixture did not start within 15 seconds")
                    time.sleep(0.02)
                yield f"http://127.0.0.1:{port}", home, provider
            finally:
                server.should_exit = True
                thread.join(timeout=15)
                if thread.is_alive():
                    raise RuntimeError("Offline fixture server did not shut down")
