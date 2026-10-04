"""Short local Windows sounds for swarm transitions; no text is spoken or saved."""

from __future__ import annotations

import asyncio
from array import array
from functools import lru_cache
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable
import wave


COOLDOWN_SECONDS = 15.0
SAMPLE_RATE = 22_050
MAX_PENDING = 16
_KINDS = {
    "completion": "completion", "completed": "completion",
    "input": "input", "needsinput": "input", "needs_input": "input", "input_required": "input",
    "blocked": "blocked", "stuck": "blocked",
}
_SEQUENCES = {
    "completion": ((523.25, 0.16), (659.25, 0.16), (783.99, 0.24)),
    "input": ((880.0, 0.20), (1174.66, 0.24)),
    "blocked": ((392.0, 0.20), (329.63, 0.20), (261.63, 0.25)),
}
_GAPS = {"completion": 0.08, "input": 0.14, "blocked": 0.11}


@lru_cache(maxsize=3)
def _wave_for(kind: str) -> bytes:
    """Produce bounded mono 16-bit PCM with gentle attack/release to avoid clicks."""
    samples = array("h")
    sequence = _SEQUENCES[kind]
    for note, (frequency, duration) in enumerate(sequence):
        count = round(SAMPLE_RATE * duration)
        attack, release = SAMPLE_RATE * 0.012, SAMPLE_RATE * 0.03
        for index in range(count):
            envelope = min(1.0, index / attack, (count - 1 - index) / release)
            envelope *= math.exp(-1.4 * index / count)
            samples.append(round(7500 * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)))
        if note < len(sequence) - 1:
            samples.extend([0] * round(SAMPLE_RATE * _GAPS[kind]))
    samples.extend([0] * round(SAMPLE_RATE * 0.06))
    if sys.byteorder != "little":
        samples.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.tobytes())
    return buffer.getvalue()


def _native_player() -> Callable[[bytes], None] | None:
    if os.name != "nt":
        return None
    try:
        import winsound
    except ImportError:
        return None

    def play(data: bytes):
        # Memory playback cannot use SND_ASYNC, and winsound has no SND_SYNC
        # flag because synchronous is its default. The queue calls this in a
        # worker thread, so the blocking sound never stalls the event loop.
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)

    return play


class Notifier:
    def __init__(self, home: Path, play: Callable[[bytes], None] | None = None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "notifications.json"
        self.enabled = True
        self.last_error: str | None = None
        self._play = play if play is not None else _native_player()
        self.available = self._play is not None
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=MAX_PENDING)
        self._pending: set[tuple[str, str]] = set()
        self._recent: dict[tuple[str, str], float] = {}
        self._worker: asyncio.Task | None = None
        self._closed = False
        try:
            if self.path.exists():
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or type(saved.get("enabled")) is not bool:
                    raise ValueError("Invalid preference")
                self.enabled = saved["enabled"]
            else:
                self._save(self.enabled)
        except (OSError, ValueError, TypeError):
            self.last_error = "Saved notification preferences could not be read or created."

    def public(self) -> dict:
        return {"enabled": self.enabled, "available": self.available, "last_error": self.last_error}

    def _save(self, enabled: bool):
        descriptor, temporary = tempfile.mkstemp(prefix=".notifications-", suffix=".tmp", dir=self.home)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"enabled": enabled}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def configure(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("Notification enabled must be true or false")
        if self._closed:
            raise ValueError("Notifications are closed")
        try:
            self._save(enabled)
        except OSError:
            self.last_error = "Notification preferences could not be saved."
            raise ValueError(self.last_error) from None
        self.enabled = enabled
        self.last_error = None
        if not enabled:
            self._discard_pending()

    async def notify(self, kind: str, run_id: str, detail: str = "") -> None:
        """Queue a transition and return immediately. Detail text is never retained."""
        normalized = _KINDS.get(kind)
        if normalized is None:
            raise ValueError("Unknown notification kind")
        if self._closed or not self.enabled or not self.available:
            return
        self._enqueue((str(run_id), normalized))

    async def test(self) -> None:
        """Explicit preview plays even when automatic alerts are disabled."""
        if self._closed:
            raise ValueError("Notifications are closed")
        if not self.available:
            raise ValueError("Native audio notifications are available only on Windows")
        self._enqueue(("__preview__", "completion"), preview=True)

    def _enqueue(self, key: tuple[str, str], *, preview=False):
        moment = time.monotonic()
        if key in self._pending:
            return
        if not preview and moment - self._recent.get(key, -float("inf")) < COOLDOWN_SECONDS:
            return
        if self._queue.full():
            return
        # Only a short cooldown is retained in memory; nothing replays at startup.
        self._recent = {item: stamp for item, stamp in self._recent.items() if moment - stamp < COOLDOWN_SECONDS}
        self._recent[key] = moment
        self._pending.add(key)
        self._queue.put_nowait(key)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._consume(), name="swarm-notification-audio")

    def _render_and_play(self, kind: str):
        self._play(_wave_for(kind))

    async def _consume(self):
        while True:
            key = await self._queue.get()
            try:
                if key is None:
                    return
                try:
                    await asyncio.to_thread(self._render_and_play, key[1])
                except Exception:
                    # Driver errors can contain machine paths; expose no raw text.
                    self.last_error = "Notification audio could not be played."
                else:
                    self.last_error = None
            finally:
                if key is not None:
                    self._pending.discard(key)
                self._queue.task_done()

    def _discard_pending(self):
        while True:
            try:
                key = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if key is not None:
                self._pending.discard(key)
            self._queue.task_done()

    async def close(self):
        if self._closed:
            if self._worker is not None:
                await asyncio.shield(self._worker)
            return
        self._closed = True
        self._discard_pending()
        if self._worker is not None and not self._worker.done():
            self._queue.put_nowait(None)
            # Native sequences last less than one second. Let the current sound
            # finish and discard the rest, rather than abandon a playback thread.
            await asyncio.shield(self._worker)
        self._pending.clear()
