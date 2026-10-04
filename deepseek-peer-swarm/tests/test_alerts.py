"""Notification API and real engine transition coverage without audible playback."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from swarm.app import NewRun, create_app
from swarm.config import PEERS, Settings
from swarm.engine import Engine
from swarm.notifications import Notifier
from swarm.provider import ProviderError
from swarm.store import Store


class QuietProvider:
    async def complete(self, *args):
        await asyncio.Event().wait()

    async def close(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, kind, run_id, detail=""):
        self.calls.append({"kind": kind, "run_id": run_id, "detail": detail})

    async def close(self):
        pass


async def wait_until(condition, seconds=4):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def alert_engine(tmp_path):
    resources = []

    async def make(*, provider=None, start_workers=False, **limits):
        home = tmp_path / f"engine-{len(resources)}"
        settings = Settings(home)
        settings.keys = [f"fake-alert-test-secret-{index}" for index in range(10)]
        store = Store(home / "swarm.sqlite")
        notifier = FakeNotifier()
        engine = Engine(store, settings, provider or QuietProvider(), notifier=notifier)
        if not start_workers:
            engine.launch = lambda state: None
        await engine.start()
        request = NewRun(task="Verify alert transitions", **limits).model_dump()
        run = await engine.create(request)
        resources.append((engine, store, notifier))
        return engine, engine.state(run["id"]), notifier

    yield make
    for engine, store, notifier in reversed(resources):
        await engine.close()
        await notifier.close()
        store.close()


async def test_notification_routes_require_token_persist_toggle_and_preview_silently(tmp_path):
    sounds = []
    notifier = Notifier(tmp_path, play=sounds.append)
    app = create_app(tmp_path, provider=QuietProvider(), notifier=notifier)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8767") as client:
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["notifications"] == {"enabled": True, "available": True, "last_error": None}
            headers = {"X-Swarm-Token": bootstrap["csrf_token"]}
            assert (await client.post("/api/notifications", json={"enabled": False})).status_code == 403
            assert (await client.post("/api/notifications/test")).status_code == 403
            denied = await client.post("/api/notifications/test", headers={**headers, "Origin": "https://foreign.example"})
            assert denied.status_code == 403
            invalid = await client.post("/api/notifications", json={"enabled": "false"}, headers=headers)
            assert invalid.status_code == 422
            disabled = await client.post("/api/notifications", json={"enabled": False}, headers=headers)
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            assert json.loads((tmp_path / "notifications.json").read_text()) == {"enabled": False}
            refreshed = (await client.get("/api/bootstrap")).json()
            assert refreshed["notifications"]["enabled"] is False
            preview = await client.post("/api/notifications/test", headers=headers)
            assert preview.status_code == 200
            await asyncio.wait_for(notifier._queue.join(), 2)
            assert len(sounds) == 1
            assert preview.json()["enabled"] is False
    assert notifier._closed
    assert notifier._worker.done()


async def test_reviewed_completion_emits_one_completion_alert(alert_engine):
    engine, state, notifier = await alert_engine()
    item = (await engine.execute(state, PEERS[0], "create_work", {"title": "Review output", "description": "Verify completion"}))["item"]
    await engine.execute(state, PEERS[0], "claim_work", {"item_id": item["id"]})
    await engine.execute(state, PEERS[0], "finish_work", {"item_id": item["id"], "result": "Synthetic result verified"})
    await engine.execute(state, PEERS[1], "review_work", {"item_id": item["id"], "approved": True, "evidence": "Independent review complete"})
    for peer in PEERS:
        agent = engine.agent(state, peer)
        agent["seen_revision"] = state["revision"]
        await engine.execute(state, peer, "vote_complete", {"evidence": "Reviewed work satisfies the objective"})
    await wait_until(lambda: state["run"]["status"] == "completed")
    await asyncio.sleep(1.05)
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["kind"] in {"completion", "completed"}
    assert notifier.calls[0]["run_id"] == state["run"]["id"]


async def test_peer_waiting_for_input_alerts_without_waiting_for_other_peers(alert_engine):
    engine, state, notifier = await alert_engine()
    agent = engine.agent(state, PEERS[0])
    agent["seen_user_revision"] = state["user_revision"]
    result = await engine.execute(state, PEERS[0], "wait_for_input", {"question": "Which experiment should we run next?"})
    assert result["ok"] is True
    await wait_until(lambda: bool(notifier.calls))
    assert notifier.calls[-1]["kind"] in {"input", "needs_input", "input_required"}
    assert state["run"]["status"] == "running"
    assert all(a["wait_revision"] == -1 for a in state["agents"][1:])


async def test_pending_command_approval_alerts_immediately_and_redacts_details(alert_engine):
    engine, state, notifier = await alert_engine()
    secret = engine.settings.keys[0]
    waiting = asyncio.create_task(engine.approve(state, PEERS[0], "commands", {
        "command": "echo " + secret, "cwd": state["run"]["workspace"], "timeout": 120,
    }))
    try:
        await wait_until(lambda: bool(notifier.calls))
        assert notifier.calls[0]["kind"] in {"input", "needs_input", "input_required"}
        assert secret not in json.dumps(notifier.calls)
        assert not waiting.done()
        approval = state["approvals"][0]
        engine.resolve_approval(state["run"]["id"], approval["id"], False)
        assert await waiting is False
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_fatal_provider_failure_pauses_and_emits_blocked_alert(alert_engine):
    class FatalProvider(QuietProvider):
        async def complete(self, key, *args):
            raise ProviderError("Permanent provider failure involving " + key, retryable=False)

    engine, state, notifier = await alert_engine(provider=FatalProvider(), start_workers=True)
    await wait_until(lambda: state["run"]["status"] == "paused")
    assert notifier.calls
    assert notifier.calls[-1]["kind"] == "blocked"
    assert state["run"].get("pause_reason")
    for key in engine.settings.keys:
        assert key not in json.dumps(notifier.calls)


async def test_time_budget_pause_emits_blocked_alert_and_records_reason(alert_engine):
    engine, state, notifier = await alert_engine(max_minutes=1)
    state["run"]["active_seconds"] = 60
    await wait_until(lambda: state["run"]["status"] == "paused")
    assert notifier.calls[-1]["kind"] == "blocked"
    assert "time" in state["run"]["pause_reason"].lower()


async def test_stalled_run_pauses_and_emits_blocked_alert(alert_engine):
    engine, state, notifier = await alert_engine()
    state["run"]["stall_minutes"] = 1
    state["run"]["active_seconds"] = 61
    state["progress_seconds"] = 0
    await wait_until(lambda: state["run"]["status"] == "paused")
    assert notifier.calls[-1]["kind"] == "blocked"
    reason = state["run"]["pause_reason"].lower()
    assert "progress" in reason or "stall" in reason


async def test_explicit_user_pause_does_not_sound_like_failure(alert_engine):
    engine, state, notifier = await alert_engine()
    await engine.control(state["run"]["id"], "pause")
    assert state["run"]["status"] == "paused"
    assert notifier.calls == []
