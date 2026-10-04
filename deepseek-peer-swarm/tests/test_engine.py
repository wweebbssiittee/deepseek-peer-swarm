"""Behavioral tests for coordination, interrupted work and provider boundaries."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
import pytest_asyncio

from swarm.a2a import dispatch_a2a, peer_message
from swarm.app import NewRun
from swarm.config import PEERS, Settings
from swarm.engine import Engine
from swarm.provider import DeepSeek, ProviderError
from swarm.store import Store


class NoNetwork:
    async def complete(self, *args):
        raise AssertionError("This test must never call an external provider")

    async def close(self):
        pass


class GatedProvider(NoNetwork):
    def __init__(self, name="checkpoint", arguments=None):
        self.calls = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()
        self.name = name
        self.arguments = arguments or {"summary": "Provider test checkpoint"}

    async def complete(self, key, config, messages, schemas):
        self.calls.append((key, deepcopy(messages)))
        index = len(self.calls)
        if index == 10:
            self.all_started.set()
        await self.release.wait()
        return {
            "role": "assistant", "content": None,
            "reasoning_content": "opaque-provider-continuation",
            "tool_calls": [{"id": f"tool-{index}", "type": "function", "function": {
                "name": self.name, "arguments": json.dumps(self.arguments),
            }}],
        }, {"total_tokens": 100}


@pytest_asyncio.fixture
async def make_engine(tmp_path):
    resources = []

    async def make(provider=None, **limits):
        home = tmp_path / f"instance-{len(resources)}"
        settings = Settings(home)
        settings.keys = [f"fake-private-key-slot-{i:02d}" for i in range(10)]
        store = Store(home / "swarm.sqlite")
        engine = Engine(store, settings, provider or NoNetwork())
        # Board-only tests control exactly when real workers start.
        engine.launch = lambda state: None
        resources.append((engine, store))
        request = NewRun(task="Verify a long-running collaborative experiment", **limits).model_dump()
        run = await engine.create(request)
        return engine, engine.state(run["id"])

    yield make
    for engine, store in reversed(resources):
        await engine.close()
        store.close()


async def reviewed_item(engine, state):
    item = (await engine.execute(state, PEERS[0], "create_work", {
        "title": "Check experiment output", "description": "Run and independently verify the experiment.",
    }))["item"]
    await engine.execute(state, PEERS[0], "claim_work", {"item_id": item["id"]})
    await engine.execute(state, PEERS[0], "finish_work", {"item_id": item["id"], "result": "Measured output equals expected output."})
    await engine.execute(state, PEERS[1], "review_work", {"item_id": item["id"], "approved": True, "evidence": "Independently reproduced the result."})
    return item


@pytest.mark.asyncio
async def test_actual_peer_tool_uses_a2a_dispatch_and_delivers_trusted_identity(make_engine):
    engine, state = await make_engine()
    revision = state["revision"]
    response = await engine.execute(state, "peer-03", "peer_message", {"to": "peer-07", "text": "Review the measured output while I check the source."})
    assert response["jsonrpc"] == "2.0"
    assert "result" in response and "error" not in response
    assert state["messages"][-1]["from"] == "peer-03"
    assert state["messages"][-1]["to"] == "peer-07"
    assert state["revision"] == revision
    assert engine.wakes[(state["run"]["id"], "peer-07")].is_set()


@pytest.mark.asyncio
async def test_competing_claims_have_exactly_one_owner_and_require_independent_review(make_engine):
    engine, state = await make_engine()
    item = (await engine.execute(state, PEERS[0], "create_work", {"title": "Shared experiment", "description": "Measure once"}))["item"]
    results = await asyncio.gather(*[
        engine.execute(state, peer, "claim_work", {"item_id": item["id"]}) for peer in PEERS
    ], return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    owner = item["owner"]
    reviewer = next(peer for peer in PEERS if peer != owner)
    with pytest.raises(ValueError):
        await engine.execute(state, reviewer, "finish_work", {"item_id": item["id"], "result": "unowned work"})
    await engine.execute(state, owner, "finish_work", {"item_id": item["id"], "result": "Measured artifact"})
    with pytest.raises(ValueError):
        await engine.execute(state, owner, "review_work", {"item_id": item["id"], "approved": True, "evidence": "Self review"})
    await engine.execute(state, reviewer, "review_work", {"item_id": item["id"], "approved": False, "evidence": "Observed a mismatching output"})
    assert item["status"] == "open" and item["owner"] is None
    await engine.execute(state, reviewer, "claim_work", {"item_id": item["id"]})
    assert engine.store.get(state["run"]["id"])["items"][0]["owner"] == reviewer


@pytest.mark.asyncio
async def test_user_steering_invalidates_previous_completion_votes(make_engine):
    engine, state = await make_engine()
    await reviewed_item(engine, state)
    for peer in PEERS:
        engine.agent(state, peer)["seen_revision"] = state["revision"]
        await engine.execute(state, peer, "vote_complete", {"evidence": "Checked the artifact"})
    assert all(agent["vote"] == state["revision"] for agent in state["agents"])
    engine.message(state, "user", "all", "Also verify a second dataset before completion")
    assert all(agent["vote"] != state["revision"] for agent in state["agents"])


@pytest.mark.asyncio
async def test_inflight_old_responses_cannot_vote_for_unseen_user_steering(make_engine):
    provider = GatedProvider("vote_complete", {"evidence": "Verified the original dataset"})
    engine, state = await make_engine(provider, max_rounds=1)
    await reviewed_item(engine, state)
    Engine.launch(engine, state)
    await asyncio.wait_for(provider.all_started.wait(), 3)
    engine.message(state, "user", "all", "Stop completion: the experiment must also cover another dataset")
    provider.release.set()
    await asyncio.wait_for(asyncio.gather(*engine.workers[state["run"]["id"]]), 3)
    assert not any(agent["vote"] == state["revision"] for agent in state["agents"])
    assert len({key for key, _ in provider.calls}) == 10


@pytest.mark.asyncio
async def test_inflight_question_does_not_hide_a_new_user_answer(make_engine):
    provider = GatedProvider("wait_for_input", {"question": "Which dataset should I use?"})
    engine, state = await make_engine(provider, max_rounds=1)
    Engine.launch(engine, state)
    await asyncio.wait_for(provider.all_started.wait(), 3)
    engine.message(state, "user", "all", "Use dataset B; continue without further input")
    provider.release.set()
    await asyncio.wait_for(asyncio.gather(*engine.workers[state["run"]["id"]]), 3)
    assert all(agent["wait_revision"] != state["user_revision"] for agent in state["agents"])


@pytest.mark.asyncio
async def test_parallel_requests_reserve_budget_and_cancellation_keeps_uncertain_cost(make_engine):
    provider = GatedProvider()
    # Learned reservations are far smaller than the old byte bound, so the
    # limit must be tighter for ten peers to contend for it.
    engine, state = await make_engine(provider, max_tokens=20_000)
    Engine.launch(engine, state)
    async with asyncio.timeout(3):
        while not provider.calls:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    reserved = state["usage"]["reserved_tokens"]
    assert 0 < len(provider.calls) < 10
    assert 0 < reserved <= state["run"]["max_tokens"]
    assert state["usage"]["total_tokens"] == 0
    await engine.control(state["run"]["id"], "pause")
    assert state["usage"]["reserved_tokens"] == 0
    assert state["usage"]["uncertain_tokens"] == reserved
    persisted = engine.store.get(state["run"]["id"])
    assert persisted["usage"] == state["usage"]


@pytest.mark.asyncio
async def test_cancel_expires_pending_approval_and_releases_claim(make_engine):
    engine, state = await make_engine()
    item = (await engine.execute(state, PEERS[0], "create_work", {"title": "Deployment", "description": "Await owner approval"}))["item"]
    await engine.execute(state, PEERS[0], "claim_work", {"item_id": item["id"]})
    task = asyncio.create_task(engine.approve(state, PEERS[0], "deploy", {"command": "example"}))
    engine.workers[state["run"]["id"]] = [task]
    await asyncio.sleep(0)
    approval = state["approvals"][0]
    assert approval["status"] == "pending"
    await engine.control(state["run"]["id"], "stop")
    assert task.cancelled()
    assert approval["status"] == "expired" and not engine.approval_waiters
    assert item["status"] == "open" and item["owner"] is None
    with pytest.raises(ValueError):
        engine.resolve_approval(state["run"]["id"], approval["id"], True)


@pytest.mark.asyncio
async def test_restart_does_not_replay_work_and_accounts_for_lost_provider_requests(make_engine):
    engine, state = await make_engine()
    item = (await engine.execute(state, PEERS[0], "create_work", {"title": "Interrupted experiment", "description": "Check outcome after restart"}))["item"]
    await engine.execute(state, PEERS[0], "claim_work", {"item_id": item["id"]})
    state["approvals"].append({"id": "lost-approval", "status": "pending"})
    state["usage"].update(reserved_tokens=1234, uncertain_tokens=56)
    engine.save(state)
    recovered = Engine(engine.store, engine.settings, NoNetwork())
    try:
        await recovered.start()
        restored = recovered.state(state["run"]["id"])
        assert restored["run"]["status"] == "paused" and not recovered.workers
        assert restored["items"][0]["status"] == "open"
        assert restored["approvals"][0]["status"] == "expired"
        assert restored["usage"]["reserved_tokens"] == 0
        assert restored["usage"]["uncertain_tokens"] == 1290
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_deepseek_reasoning_and_tool_results_survive_next_request_and_checkpoint(make_engine):
    class ScriptedProvider(NoNetwork):
        def __init__(self):
            self.calls = []

        async def complete(self, key, config, messages, schemas):
            self.calls.append(deepcopy(messages))
            if len(self.calls) == 1:
                return {
                    "role": "assistant", "content": None, "reasoning_content": "opaque-continuation-token",
                    "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "checkpoint", "arguments": '{"summary":"Experiment output recorded"}'}}],
                }, {"total_tokens": 123}
            return {"role": "assistant", "content": "Work checkpointed", "reasoning_content": "second-opaque-token"}, {"total_tokens": 234}

    provider = ScriptedProvider()
    engine, state = await make_engine(provider, max_rounds=2)
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 3)
    assert len(provider.calls) == 2
    assistant = next(message for message in provider.calls[1] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "opaque-continuation-token"
    tool = next(message for message in provider.calls[1] if message["role"] == "tool")
    assert tool["tool_call_id"] == "call-1" and json.loads(tool["content"])["ok"]
    history = engine.store.checkpoint(state["run"]["id"], PEERS[0])
    assert sum("reasoning_content" in message for message in history) == 2
    assert "opaque-continuation-token" not in json.dumps(engine.snapshot(state["run"]["id"]))
    assert state["usage"]["total_tokens"] == 357 and state["usage"]["reserved_tokens"] == 0


def test_interrupted_tool_batch_is_closed_without_reexecuting_side_effects():
    history = [
        {"role": "assistant", "content": None, "reasoning_content": "opaque-continuation", "tool_calls": [
            {"id": "completed", "type": "function", "function": {"name": "checkpoint", "arguments": "{}"}},
            {"id": "uncertain-deploy", "type": "function", "function": {"name": "deploy_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "completed", "content": '{"ok":true}'},
    ]
    repaired = Engine.repair_history(history)
    assert len(repaired) == 3
    assert repaired[-1]["tool_call_id"] == "uncertain-deploy"
    assert "unknown" in json.loads(repaired[-1]["content"])["error"]
    assert len(Engine.repair_history(repaired)) == 3


@pytest.mark.asyncio
async def test_a2a_steers_existing_run_without_spoofing_peer_identity(make_engine):
    engine, state = await make_engine()
    state["run"]["status"] = "waiting"
    original_revision = state["user_revision"]
    envelope = peer_message("peer-01", "peer-02", state["run"]["id"], "External user instruction")
    result = await dispatch_a2a(engine, envelope, "peer-02")
    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert state["messages"][-1]["from"] == "external"
    assert state["messages"][-1]["to"] == "peer-02"
    assert state["user_revision"] == original_revision + 1
    assert result["result"]["task"]["history"][-1]["messageId"] == envelope["params"]["message"]["messageId"]
    await dispatch_a2a(engine, envelope, "peer-02")
    assert state["user_revision"] == original_revision + 1
    envelope["params"]["message"]["contextId"] = "wrong-context"
    assert (await dispatch_a2a(engine, envelope))["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_a2a_task_times_are_stable_and_terminal_errors_are_semantic(make_engine):
    engine, state = await make_engine()
    run_id = state["run"]["id"]
    first = (await engine.a2a_get({"id": run_id}))["status"]["timestamp"]
    await asyncio.sleep(0.01)
    assert (await engine.a2a_get({"id": run_id}))["status"]["timestamp"] == first
    assert (await engine.a2a_list({"statusTimestampAfter": "2099-01-01T00:00:00Z"}))["tasks"] == []
    state["run"]["status"] = "completed"
    engine.save(state)
    cancel = await dispatch_a2a(engine, {"jsonrpc": "2.0", "id": 1, "method": "CancelTask", "params": {"id": run_id}})
    assert cancel["error"]["code"] == -32002
    message = peer_message("peer-01", "all", run_id, "Continue")
    assert (await dispatch_a2a(engine, message))["error"]["code"] == -32004
    message["params"]["message"].pop("taskId")
    message["params"]["message"].pop("contextId")
    assert (await dispatch_a2a(engine, message))["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_a2a_cursor_is_stable_when_a_newer_task_is_inserted(make_engine):
    engine, state = await make_engine()
    state["run"].update(status="paused", status_updated_at="2026-01-01T00:00:00+00:00")
    for number in range(2, 5):
        other = deepcopy(state)
        other["run"].update(id=f"run-{number}", status_updated_at=f"2026-01-0{number}T00:00:00+00:00")
        engine.states[other["run"]["id"]] = other
    first = await engine.a2a_list({"pageSize": 2})
    assert [task["id"] for task in first["tasks"]] == ["run-4", "run-3"]
    newest = deepcopy(state)
    newest["run"].update(id="newest", status_updated_at="2026-02-01T00:00:00+00:00")
    engine.states["newest"] = newest
    second = await engine.a2a_list({"pageSize": 2, "pageToken": first["nextPageToken"]})
    assert [task["id"] for task in second["tasks"]] == ["run-2", state["run"]["id"]]
    assert second["nextPageToken"] == ""
    invalid = await dispatch_a2a(engine, {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {"pageToken": "not-a-cursor"}})
    assert invalid["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_deepseek_http_adapter_preserves_reasoning_content_without_network():
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer fake-key-only"
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": None, "reasoning_content": "new-opaque-continuation",
                "tool_calls": [{"id": "new-call", "type": "function", "function": {"name": "checkpoint", "arguments": "{}"}}],
                "irrelevant_provider_field": "discard",
            }}],
            "usage": {"total_tokens": 17},
        })

    provider = DeepSeek()
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    messages = [
        {"role": "user", "content": "Continue the experiment"},
        {"role": "assistant", "content": None, "reasoning_content": "prior-opaque-continuation", "tool_calls": [
            {"id": "prior-call", "type": "function", "function": {"name": "checkpoint", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "prior-call", "content": '{"ok":true}'},
    ]
    try:
        result, usage = await provider.complete("fake-key-only", {"model": "configured-model", "thinking": True, "reasoning_effort": "high"}, messages, [])
        await provider.complete("fake-key-only", {"model": "configured-model", "thinking": False, "max_output_tokens": 16384}, messages, [])
    finally:
        await provider.close()
    assert observed[0]["messages"][1]["reasoning_content"] == "prior-opaque-continuation"
    assert observed[0]["messages"][-1]["tool_call_id"] == "prior-call"
    assert observed[0]["max_tokens"] == 8192 and observed[1]["max_tokens"] == 16384
    assert observed[0]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in observed[1]
    assert result["reasoning_content"] == "new-opaque-continuation"
    assert "irrelevant_provider_field" not in result and usage["total_tokens"] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (429, True), (503, True)])
async def test_deepseek_errors_never_expose_response_bodies_or_credentials(status, retryable):
    provider = DeepSeek()
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="fake-private-api-key C:/private/secret-file")
    ))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.complete("fake-private-api-key", {"model": "test", "thinking": False}, [], [])
    finally:
        await provider.close()
    assert caught.value.retryable is retryable
    assert "fake-private-api-key" not in str(caught.value)
    assert "secret-file" not in str(caught.value)


@pytest.mark.asyncio
async def test_deepseek_network_error_is_sanitized():
    def broken(request):
        raise httpx.ConnectError("fake-private-api-key C:/private/secret-file", request=request)

    provider = DeepSeek()
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.complete("fake-private-api-key", {"model": "test", "thinking": False}, [], [])
    finally:
        await provider.close()
    assert caught.value.retryable
    assert "fake-private-api-key" not in str(caught.value) and "secret-file" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [None, [], "unstructured-secret-provider-data", {"tool_calls": {}}, {"tool_calls": [{"id": "bad"}]}])
async def test_deepseek_invalid_message_shape_is_a_sanitized_provider_error(message):
    provider = DeepSeek()
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": message}]})
    ))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.complete("fake-key", {"model": "test", "thinking": False}, [], [])
    finally:
        await provider.close()
    assert caught.value.retryable
    assert "unstructured-secret-provider-data" not in str(caught.value)


@pytest.mark.asyncio
async def test_output_limit_cannot_silently_return_truncated_tool_calls():
    provider = DeepSeek()
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {
                "role": "assistant", "content": None, "reasoning_content": "opaque-continuation",
                "tool_calls": [{"id": "partial", "type": "function", "function": {
                    "name": "deploy_command", "arguments": '{"command":"incomplete',
                }}],
            }}],
            "usage": {"total_tokens": 4096},
        })
    ))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.complete("fake-key", {"model": "test", "thinking": True, "reasoning_effort": "high"}, [], [])
    finally:
        await provider.close()
    assert not caught.value.retryable
    assert "limit" in str(caught.value).lower() or "truncat" in str(caught.value).lower()
    assert caught.value.usage == {"total_tokens": 4096}


@pytest.mark.asyncio
async def test_known_usage_on_failed_provider_response_is_settled_exactly(make_engine):
    class LimitedProvider(NoNetwork):
        async def complete(self, *args):
            raise ProviderError("Output limit exceeded", retryable=False, usage={"total_tokens": 77})

    engine, state = await make_engine(LimitedProvider(), max_rounds=1)
    await engine._worker(state, PEERS[0])
    assert state["usage"]["total_tokens"] == 77
    assert state["usage"]["reserved_tokens"] == state["usage"]["uncertain_tokens"] == 0
    assert engine.agent(state, PEERS[0])["tokens"] == 77
    assert engine.agent(state, PEERS[0])["status"] == "error"
