import asyncio
from array import array
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import pytest

from swarm.notifications import COOLDOWN_SECONDS, Notifier, _wave_for


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.played = []
        self.notifier = Notifier(self.home, play=self.played.append)

    async def asyncTearDown(self):
        await self.notifier.close()
        self.temporary.cleanup()

    async def drain(self, notifier=None):
        await asyncio.wait_for((notifier or self.notifier)._queue.join(), 3)

    def test_distinct_sounds_are_short_valid_pcm_with_gentle_boundaries(self):
        waveforms = [_wave_for(kind) for kind in ("completion", "input", "blocked")]
        self.assertEqual(len(set(waveforms)), 3)
        for data in waveforms:
            with wave.open(io.BytesIO(data), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 1)
                self.assertEqual(audio.getsampwidth(), 2)
                self.assertEqual(audio.getframerate(), 22050)
                duration = audio.getnframes() / audio.getframerate()
                self.assertGreater(duration, 0.4)
                self.assertLess(duration, 1.0)
                samples = array("h", audio.readframes(audio.getnframes()))
            self.assertEqual(samples[0], 0)
            self.assertEqual(samples[-1], 0)
            self.assertGreater(max(abs(value) for value in samples), 3000)
            self.assertLessEqual(max(abs(value) for value in samples), 7500)
            self.assertGreater(sum(value == 0 for value in samples), 1000)

    async def test_default_enabled_preference_is_persisted_without_event_details(self):
        self.assertEqual(self.notifier.public(), {"enabled": True, "available": True, "last_error": None})
        self.assertEqual(json.loads((self.home / "notifications.json").read_text()), {"enabled": True})
        await self.notifier.notify("completion", "run-id", "do not retain this secret detail")
        await self.drain()
        self.assertEqual(len(self.played), 1)
        self.assertNotIn("secret", (self.home / "notifications.json").read_text())
        self.assertNotIn("run-id", (self.home / "notifications.json").read_text())

    async def test_disabled_preference_survives_restart_and_suppresses_automatic_sound(self):
        self.notifier.configure(False)
        await self.notifier.notify("completion", "run-id")
        self.assertEqual(self.played, [])
        restarted = Notifier(self.home, play=self.played.append)
        try:
            self.assertFalse(restarted.public()["enabled"])
            await restarted.notify("input", "run-id")
            self.assertEqual(self.played, [])
            self.assertIsNone(restarted._worker)
            restarted.configure(True)
            await restarted.notify("input", "run-id")
            await self.drain(restarted)
            self.assertEqual(len(self.played), 1)
        finally:
            await restarted.close()

    async def test_test_button_explicitly_previews_when_automatic_alerts_are_disabled(self):
        self.notifier.configure(False)
        await self.notifier.test()
        await self.drain()
        self.assertEqual(self.played, [_wave_for("completion")])
        self.assertFalse(self.notifier.public()["enabled"])

    async def test_ten_peers_coalesce_and_cooldown_is_per_run_and_kind(self):
        await asyncio.gather(*(self.notifier.notify("needs_input", "same-run", f"peer-{index}") for index in range(10)))
        await self.drain()
        self.assertEqual(len(self.played), 1)
        await self.notifier.notify("input", "same-run")
        await self.drain()
        self.assertEqual(len(self.played), 1)
        await self.notifier.notify("blocked", "same-run")
        await self.notifier.notify("input", "another-run")
        await self.drain()
        self.assertEqual(len(self.played), 3)
        self.notifier._recent[("same-run", "input")] = time.monotonic() - COOLDOWN_SECONDS - 0.1
        await self.notifier.notify("input", "same-run")
        await self.drain()
        self.assertEqual(len(self.played), 4)

    async def test_notifications_return_immediately_and_do_not_block_event_loop(self):
        started, release = threading.Event(), threading.Event()
        playback_threads = []

        def blocking_player(data):
            playback_threads.append(threading.current_thread())
            started.set()
            release.wait(2)

        self.notifier._play = blocking_player
        try:
            await asyncio.wait_for(self.notifier.notify("completion", "run-id"), 0.2)
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 2))
            heartbeat = []

            async def beat():
                await asyncio.sleep(0)
                heartbeat.append(True)

            await asyncio.wait_for(beat(), 0.2)
            self.assertEqual(heartbeat, [True])
            self.assertIsNot(playback_threads[0], threading.current_thread())
            closing = asyncio.create_task(self.notifier.close())
            await asyncio.sleep(0.01)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 2)
        finally:
            release.set()

    async def test_close_drops_queued_notifications_and_does_not_abandon_current_playback(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def blocking_player(data):
            calls.append(data)
            started.set()
            release.wait(2)

        self.notifier._play = blocking_player
        try:
            await self.notifier.notify("completion", "first-run")
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await self.notifier.notify("input", "second-run")
            await self.notifier.notify("blocked", "third-run")
            closing = asyncio.create_task(self.notifier.close())
            await asyncio.sleep(0.01)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(self.notifier._worker.done())
            self.assertEqual(self.notifier._pending, set())
            self.assertTrue(self.notifier._queue.empty())
            await self.notifier.notify("completion", "after-close")
            self.assertEqual(len(calls), 1)
        finally:
            release.set()

    async def test_playback_is_serial_even_for_different_events(self):
        active = 0
        maximum = 0
        calls = []

        def serial_probe(data):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.02)
            calls.append(data)
            active -= 1

        self.notifier._play = serial_probe
        for kind in ("completion", "input", "blocked"):
            await self.notifier.notify(kind, "same-run")
        await self.drain()
        self.assertEqual(maximum, 1)
        self.assertEqual(len(calls), 3)

    async def test_playback_failures_are_safe_and_worker_recovers(self):
        def failing_player(data):
            raise RuntimeError("sk-secret-value at C:/private/user-info")

        self.notifier._play = failing_player
        await self.notifier.notify("completion", "run-id")
        await self.drain()
        error = self.notifier.public()["last_error"]
        self.assertEqual(error, "Notification audio could not be played.")
        self.assertNotIn("secret", error)
        self.notifier._play = self.played.append
        await self.notifier.notify("blocked", "run-id")
        await self.drain()
        self.assertEqual(len(self.played), 1)
        self.assertIsNone(self.notifier.public()["last_error"])

    async def test_unavailable_native_audio_is_reported_and_never_queued(self):
        with patch("swarm.notifications._native_player", return_value=None):
            unavailable = Notifier(self.home)
        try:
            self.assertFalse(unavailable.public()["available"])
            await unavailable.notify("completion", "run-id")
            self.assertIsNone(unavailable._worker)
            with self.assertRaisesRegex(ValueError, "Windows"):
                await unavailable.test()
        finally:
            await unavailable.close()

    async def test_bad_preferences_and_write_errors_never_expose_paths(self):
        (self.home / "notifications.json").write_text("malformed secret content", encoding="utf-8")
        recovered = Notifier(self.home, play=self.played.append)
        try:
            self.assertTrue(recovered.enabled)
            self.assertNotIn("secret", recovered.public()["last_error"])
            with patch.object(recovered, "_save", side_effect=OSError("C:/private/secrets")):
                with self.assertRaisesRegex(ValueError, "preferences could not be saved"):
                    recovered.configure(False)
            self.assertTrue(recovered.enabled)
            self.assertNotIn("private", recovered.public()["last_error"])
            recovered.configure(False)
            self.assertFalse(recovered.enabled)
            self.assertIsNone(recovered.public()["last_error"])
        finally:
            await recovered.close()

    async def test_restart_does_not_replay_prior_events(self):
        await self.notifier.notify("completion", "previous-run")
        await self.drain()
        restarted_calls = []
        restarted = Notifier(self.home, play=restarted_calls.append)
        try:
            await asyncio.sleep(0)
            self.assertEqual(restarted_calls, [])
            self.assertIsNone(restarted._worker)
        finally:
            await restarted.close()


if __name__ == "__main__":
    unittest.main()


@pytest.mark.skipif(os.name != "nt", reason="winsound is Windows-only")
def test_native_player_uses_flags_winsound_actually_defines():
    """The real player is never exercised by the fake-play tests, so the flag
    names it passes to winsound must be checked directly."""
    import winsound
    from swarm.notifications import _native_player, _wave_for

    play = _native_player()
    assert play is not None
    # SND_SYNC does not exist; naming it raises AttributeError and silences
    # every alert. Synchronous playback is winsound's default.
    assert not hasattr(winsound, "SND_SYNC")
    calls = []
    original = winsound.PlaySound
    winsound.PlaySound = lambda data, flags: calls.append((data, flags))
    try:
        play(_wave_for("completion"))
    finally:
        winsound.PlaySound = original
    assert len(calls) == 1
    data, flags = calls[0]
    assert data.startswith(b"RIFF")
    assert flags == winsound.SND_MEMORY | winsound.SND_NODEFAULT
    assert not flags & winsound.SND_ASYNC
