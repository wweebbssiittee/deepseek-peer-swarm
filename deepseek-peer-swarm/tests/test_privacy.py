"""Credential privacy at public boundaries; no network or real credentials."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
import pytest_asyncio

from swarm.app import create_app
from swarm.config import PEERS, Settings
from swarm.notifications import Notifier
from swarm.provider import ProviderError


class NoNetwork:
    async def complete(self, *args):
        raise AssertionError("Privacy tests must not make provider calls")

    async def close(self):
        pass


@pytest_asyncio.fixture
async def local_app(tmp_path):
    notifier = Notifier(tmp_path, play=lambda _: None)
    app = create_app(tmp_path, provider=NoNetwork(), notifier=notifier)
    engine, settings = app.state.engine, app.state.settings
    settings.keys = [f"fake-privacy-key-slot-{index:02}" for index in range(10)]
    engine.launch = lambda state: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8767") as client:
        client.headers["X-Swarm-Token"] = settings.token
        created = await client.post("/api/runs", json={"task": "Check public privacy boundaries"})
        assert created.status_code == 200
        state = engine.state(created.json()["id"])
        yield app, client, state
    await engine.close()
    await notifier.close()
    engine.store.close()


def test_recursive_redaction_handles_json_escaping_and_does_not_mutate(tmp_path):
    settings = Settings(tmp_path)
    short = "fake-prefix-key"
    escaped = 'fake-quoted-"-backslash-\\-unicode-\u03bb-key'
    settings.keys = [short, short + "-longer", escaped] + [""] * 7
    raw = {"nested": [{"text": escaped, "overlap": short + "-longer"}], escaped: (settings.token, 7)}
    original = deepcopy(raw)
    clean = settings.redact_value(raw)
    assert clean == {"nested": [{"text": "[REDACTED]", "overlap": "[REDACTED]"}], "[REDACTED]": ("[REDACTED]", 7)}
    assert raw == original
    json.dumps(clean)


def test_loaded_settings_only_expose_supported_fields(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "model": "deepseek-flash", "keys": ["fake-legacy-plaintext-secret"],
        "password": "fake-unrelated-private-value", "internal_notes": "private",
    }), encoding="utf-8")
    settings = Settings(tmp_path)
    assert not {"keys", "password", "internal_notes"}.intersection(settings.values)
    assert not {"keys", "password", "internal_notes"}.intersection(settings.public())
    settings.update({"thinking": False})
    saved = json.loads(settings.path.read_text(encoding="utf-8"))
    assert not {"keys", "password", "internal_notes"}.intersection(saved)


def test_loaded_prompt_redacts_configured_environment_key(tmp_path, monkeypatch):
    secret = "fake-loaded-prompt-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY_1", secret)
    (tmp_path / "settings.json").write_text(json.dumps({"system_prompt": "Prompt " + secret}), encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.keys[0] == secret
    assert settings.values["system_prompt"] == "Prompt [REDACTED]"


def test_key_rotation_sanitizes_old_and_new_prompt_credentials(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    settings.keys = [f"fake-old-rotation-key-{index:02}" for index in range(10)]
    rotated = [f"fake-new-rotation-key-{index:02}" for index in range(10)]
    prompt = "Prompt " + settings.keys[0] + " " + rotated[0]
    # This test covers update ordering, not DPAPI (covered by the Windows test).
    monkeypatch.setattr("swarm.config.protect", lambda data: b"fake-sealed-vault")
    settings.update({"keys": rotated, "system_prompt": prompt})
    assert settings.keys == rotated
    assert settings.values["system_prompt"] == "Prompt [REDACTED] [REDACTED]"
    assert "fake-old-rotation-key" not in settings.path.read_text(encoding="utf-8")
    assert "fake-new-rotation-key" not in settings.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_settings_prompt_never_returns_or_saves_known_credentials(local_app):
    app, client, state = local_app
    settings = app.state.settings
    await client.post(f"/api/runs/{state['run']['id']}/control", json={"action": "pause"})
    prompt = "Follow the owner's task and respect permission checks. " * 3 + settings.keys[0] + " " + settings.token
    saved = await client.post("/api/settings", json={"system_prompt": prompt})
    assert saved.status_code == 200
    assert settings.keys[0] not in saved.text and settings.token not in saved.text
    assert "[REDACTED]" in saved.json()["system_prompt"]
    persisted = settings.path.read_text(encoding="utf-8")
    assert settings.keys[0] not in persisted and settings.token not in persisted
    bootstrap = (await client.get("/api/bootstrap")).json()
    assert settings.keys[0] not in json.dumps(bootstrap)
    # The owner's local mutation token is intentionally bootstrapped separately.
    assert bootstrap["csrf_token"] == settings.token
    assert settings.token not in json.dumps(bootstrap["settings"])


@pytest.mark.asyncio
async def test_board_and_durable_memory_are_sanitized_before_persistence(local_app):
    app, _, state = local_app
    engine, settings = app.state.engine, app.state.settings
    secret = settings.keys[0]
    item = (await engine.execute(state, PEERS[0], "create_work", {
        "title": "Inspect " + secret, "description": secret,
    }))["item"]
    await engine.execute(state, PEERS[0], "claim_work", {"item_id": item["id"]})
    await engine.execute(state, PEERS[0], "finish_work", {"item_id": item["id"], "result": secret})
    await engine.execute(state, PEERS[1], "review_work", {"item_id": item["id"], "approved": True, "evidence": secret})
    await engine.execute(state, PEERS[0], "checkpoint", {"summary": secret})
    stored = engine.store.get(state["run"]["id"])
    assert secret not in json.dumps(stored)
    assert item["result"] == item["review"] == "[REDACTED]"
    assert engine.agent(state, PEERS[0])["memory"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_legacy_state_and_ledgers_are_sanitized_on_every_public_surface(local_app):
    app, client, state = local_app
    engine, settings = app.state.engine, app.state.settings
    secret = settings.keys[0]
    run_id = state["run"]["id"]
    state["run"]["task"] = secret
    state["agents"][0].update(memory=secret, last_message=secret)
    state["items"].append({"id": "old-item", "title": secret, "description": secret,
                           "result": secret, "review": secret, "status": "done", "owner": PEERS[0]})
    state["approvals"].append({"id": "old-approval", "payload": {"command": secret}, "status": "expired"})
    state["messages"][0]["text"] = secret
    engine.store.event(run_id, "legacy", data={"text": secret})
    engine.store.record_call(state, {"id": "old-call", "status": "settled", "note": secret})
    for path in ["/api/bootstrap", f"/api/runs/{run_id}", f"/api/runs/{run_id}/export", f"/api/runs/{run_id}/costs"]:
        response = await client.get(path)
        assert response.status_code == 200
        assert secret not in response.text, path
    for method, params in [("GetTask", {"id": run_id}), ("ListTasks", {"includeArtifacts": True})]:
        response = await client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": "privacy-request", "method": method, "params": params,
        })
        assert "result" in response.json()
        assert secret not in response.text
    assert secret not in engine.context(state, PEERS[0])
    # Output filtering must never rewrite stored actions or private state.
    assert state["approvals"][0]["payload"]["command"] == secret
    assert state["run"]["task"] == secret


@pytest.mark.asyncio
async def test_mutation_responses_sanitize_existing_run_text(local_app):
    app, client, state = local_app
    secret = app.state.settings.keys[0]
    state["run"]["task"] = secret
    run_id = state["run"]["id"]
    mutations = [
        ("control", {"action": "pause"}),
        ("permissions", {"read_files": True, "write_files": False, "internet": False, "commands": "deny", "deploy": "deny"}),
        ("limits", {"max_rounds": 10, "max_tokens": 1000000, "max_minutes": 60}),
        ("control", {"action": "resume"}),
    ]
    for suffix, payload in mutations:
        response = await client.post(f"/api/runs/{run_id}/{suffix}", json=payload)
        assert response.status_code == 200
        assert secret not in response.text
    assert state["run"]["task"] == secret


@pytest.mark.asyncio
async def test_approval_redaction_keeps_original_action_and_decision(local_app):
    app, client, state = local_app
    engine, settings = app.state.engine, app.state.settings
    payload = {"command": "echo " + settings.keys[0], "cwd": state["run"]["workspace"], "timeout": 5}
    waiting = asyncio.create_task(engine.approve(state, PEERS[0], "commands", payload))
    try:
        async with asyncio.timeout(3):
            while not engine.approval_waiters:
                await asyncio.sleep(0)
        approval = state["approvals"][-1]
        response = await client.post(f"/api/runs/{state['run']['id']}/approvals/{approval['id']}", json={"approved": True})
        assert response.status_code == 200
        assert settings.keys[0] not in response.text
        assert await waiting is True
        assert approval["payload"] == payload
        assert settings.keys[0] in payload["command"]
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_private_provider_history_remains_exact_but_checkpoint_is_redacted(local_app):
    app, _, state = local_app
    engine, settings = app.state.engine, app.state.settings
    secret = settings.keys[0]
    response = {"role": "assistant", "content": secret, "reasoning_content": "opaque " + secret,
                "tool_calls": [{"id": "exact-private-call", "type": "function", "function": {
                    "name": "checkpoint", "arguments": json.dumps({"summary": secret}),
                }}]}
    class FakeProvider(NoNetwork):
        async def complete(self, *args):
            return deepcopy(response), {"total_tokens": 17}
    engine.provider = FakeProvider()
    state["run"]["max_rounds"] = 1
    await engine._worker(state, PEERS[0])
    history = engine.store.checkpoint(state["run"]["id"], PEERS[0])
    assistant = next(message for message in history if message["role"] == "assistant")
    assert assistant == response
    tool = next(message for message in history if message["role"] == "tool")
    assert tool["tool_call_id"] == "exact-private-call"
    assert engine.agent(state, PEERS[0])["memory"] == "[REDACTED]"
    assert secret not in json.dumps(engine.snapshot(state["run"]["id"]))


@pytest.mark.asyncio
async def test_provider_error_is_sanitized_before_saving_agent_status(local_app):
    app, _, state = local_app
    engine, settings = app.state.engine, app.state.settings
    secret = settings.keys[0]
    class FailingProvider(NoNetwork):
        async def complete(self, *args):
            raise ProviderError("Failure involving " + secret, retryable=False)
    engine.provider = FailingProvider()
    await engine._worker(state, PEERS[0])
    assert secret not in engine.agent(state, PEERS[0])["last_message"]
    assert secret not in json.dumps(engine.store.get(state["run"]["id"]))


@pytest.mark.asyncio
async def test_event_redaction_sanitizes_escaped_strings(local_app):
    app, _, state = local_app
    engine, settings = app.state.engine, app.state.settings
    secret = 'fake-escaped-"-credential'
    settings.keys[0] = secret
    engine.event(state, "privacy", data={"nested": [secret]})
    event = engine.store.events(state["run"]["id"])[-1]
    assert event["data"] == {"nested": ["[REDACTED]"]}
