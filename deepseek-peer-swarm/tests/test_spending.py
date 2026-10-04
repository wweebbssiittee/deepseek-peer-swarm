"""Offline integration checks for shared USD reservations and durable charges."""

import asyncio
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest
import pytest_asyncio

from swarm.app import NewRun, create_app
from swarm.billing import cost_usage, parse_budget, pricing_for
from swarm.config import PEERS
from swarm.engine import Engine
from swarm.provider import ProviderError
from swarm.store import Store


def usage(cached=800, prompt=1000, output=250):
    return {
        "prompt_tokens": prompt, "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": prompt - cached,
        "completion_tokens_details": {"reasoning_tokens": output // 2},
    }


class FakeProvider:
    def __init__(self, measured=None, gated=False):
        self.measured = usage() if measured is None else measured
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not gated:
            self.release.set()

    async def complete(self, key, config, messages, schemas):
        self.calls.append({"key": key, "model": config["model"]})
        self.entered.set()
        await self.release.wait()
        return {
            "role": "assistant", "content": None, "reasoning_content": "opaque-test-continuation",
            "tool_calls": [{"id": f"call-{len(self.calls)}", "type": "function", "function": {
                "name": "checkpoint", "arguments": '{"summary":"Verified experiment evidence"}',
            }}],
        }, deepcopy(self.measured)

    async def close(self):
        pass


class QuietNotifier:
    async def notify(self, *args):
        pass

    def public(self):
        return {"enabled": False, "available": False, "last_error": None}

    async def close(self):
        pass


@pytest_asyncio.fixture
async def make_swarm(tmp_path):
    resources = []

    async def make(provider=None, budget="1.00", model="deepseek-flash", max_rounds=1):
        provider = provider or FakeProvider()
        app = create_app(tmp_path / f"runtime-{len(resources)}", provider=provider, notifier=QuietNotifier())
        engine = app.state.engine
        engine.settings.keys = [f"test-private-key-{number}" for number in range(10)]
        engine.settings.values["model"] = model
        engine.launch = lambda state: None
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8767",
                                   headers={"X-Swarm-Token": engine.settings.token})
        resources.append((engine, client))
        request = NewRun(task="Run a bounded research experiment", max_tokens=100_000_000,
                         max_rounds=max_rounds).model_dump()
        request["budget_usd"] = budget
        run = await engine.create(request)
        return engine, engine.state(run["id"]), client

    yield make
    for engine, client in reversed(resources):
        await engine.close()
        await client.aclose()
        engine.store.close()


def committed(bill):
    return sum(bill[key] for key in ("spent_nusd", "reserved_nusd", "uncertain_nusd"))


async def single_worker(engine, state):
    task = asyncio.create_task(engine._worker(state, PEERS[0]))
    engine.workers[state["run"]["id"]] = [task]
    return task


@pytest.mark.asyncio
async def test_ten_peers_share_one_atomic_budget_and_release_unused_reservations(make_swarm):
    provider = FakeProvider(gated=True)
    engine, state, _ = await make_swarm(provider, budget="0.01")
    Engine.launch(engine, state)
    await asyncio.wait_for(provider.entered.wait(), 2)
    await asyncio.sleep(0.05)
    in_flight = engine.store.calls(state["run"]["id"], status="reserved")
    assert 0 < len(in_flight) < 10
    assert len(in_flight) == len(provider.calls)
    assert sum(call["reserved_nusd"] for call in in_flight) == state["billing"]["reserved_nusd"]
    assert committed(state["billing"]) <= parse_budget("0.01")
    assert engine.store.get(state["run"]["id"])["billing"]["reserved_nusd"] == state["billing"]["reserved_nusd"]
    await asyncio.sleep(0.55)
    assert len(provider.calls) == state["usage"]["requests"] == len(in_flight)
    provider.release.set()
    await asyncio.wait_for(asyncio.gather(*engine.workers[state["run"]["id"]]), 6)
    records = engine.store.calls(state["run"]["id"])
    assert len(provider.calls) == len(records) == 10
    assert len({record["peer"] for record in records}) == 10
    assert all(record["status"] == "settled" for record in records)
    assert state["billing"]["spent_nusd"] == 10 * 364_800
    assert state["billing"]["reserved_nusd"] == state["billing"]["uncertain_nusd"] == 0
    assert committed(state["billing"]) <= state["billing"]["budget_nusd"]
    assert "test-private-key" not in json.dumps(records)


@pytest.mark.asyncio
async def test_each_call_uses_the_run_price_snapshot_and_preserves_cache_accounting(make_swarm):
    provider = FakeProvider()
    engine, state, _ = await make_swarm(provider)
    frozen_price = deepcopy(state["billing"]["pricing"])
    engine.settings.values["model"] = "deepseek-v4-pro"
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 2)
    record = engine.store.calls(state["run"]["id"])[0]
    assert record["pricing"] == frozen_price == pricing_for("deepseek-flash")
    assert provider.calls[0]["model"] == "deepseek-flash"
    assert record["cost_nusd"] == state["billing"]["spent_nusd"] == 364_800
    assert state["billing"]["cached_tokens"] == 800 and state["billing"]["uncached_tokens"] == 200
    assert state["billing"]["completion_tokens"] == 250
    # A duplicate settlement attempt must not charge the same completed call again.
    before = deepcopy(state["billing"])
    engine.settle_call(state, engine.agent(state, PEERS[0]), record, usage())
    assert state["billing"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [
    {}, {"total_tokens": 100},
    {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1},
    {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250,
     "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 200},
])
async def test_malformed_or_incomplete_usage_cannot_release_the_dollar_hold(make_swarm, measured):
    engine, state, _ = await make_swarm(FakeProvider(measured=measured))
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 2)
    record = engine.store.calls(state["run"]["id"])[0]
    assert record["status"] == "uncertain"
    assert state["billing"]["spent_nusd"] == state["billing"]["reserved_nusd"] == 0
    assert state["billing"]["uncertain_nusd"] == record["reserved_nusd"] > 0
    if measured.get("total_tokens") == 1:
        assert state["usage"]["total_tokens"] == 0
        assert state["usage"]["uncertain_tokens"] == record["reserved_tokens"]


@pytest.mark.asyncio
async def test_cancellation_keeps_inflight_request_as_uncertain_in_both_budgets(make_swarm):
    provider = FakeProvider(gated=True)
    engine, state, _ = await make_swarm(provider)
    task = await single_worker(engine, state)
    await asyncio.wait_for(provider.entered.wait(), 2)
    reservation = engine.store.calls(state["run"]["id"])[0]
    await engine.control(state["run"]["id"], "pause")
    assert task.cancelled()
    record = engine.store.calls(state["run"]["id"])[0]
    assert record["status"] == "uncertain" and record["cost_nusd"] is None
    assert state["billing"]["reserved_nusd"] == state["usage"]["reserved_tokens"] == 0
    assert state["billing"]["uncertain_nusd"] == reservation["reserved_nusd"]
    assert state["usage"]["uncertain_tokens"] == reservation["reserved_tokens"]


@pytest.mark.asyncio
async def test_retry_reserves_again_without_erasing_the_uncertain_first_attempt(make_swarm):
    class RetryOnce(FakeProvider):
        async def complete(self, *args):
            if not self.calls:
                self.calls.append({"failed": True})
                raise ProviderError("Simulated timeout", retryable=True)
            return await super().complete(*args)

    provider = RetryOnce()
    engine, state, _ = await make_swarm(provider, max_rounds=2)
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 5)
    records = engine.store.calls(state["run"]["id"])
    assert len(records) == 2
    uncertain = next(record for record in records if record["status"] == "uncertain")
    settled = next(record for record in records if record["status"] == "settled")
    assert state["billing"]["uncertain_nusd"] == uncertain["reserved_nusd"]
    assert state["billing"]["spent_nusd"] == settled["cost_nusd"] == 364_800
    assert state["billing"]["reserved_nusd"] == 0


@pytest.mark.asyncio
async def test_repeated_failures_cannot_retry_past_the_money_budget(make_swarm):
    class AlwaysTimeout(FakeProvider):
        async def complete(self, *args):
            self.calls.append({"failed": True})
            raise ProviderError("Simulated timeout", retryable=True)

    provider = AlwaysTimeout()
    engine, state, _ = await make_swarm(provider, budget="0.01", model="deepseek-v4-pro", max_rounds=100)
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 5)
    assert len(provider.calls) == 1
    assert state["run"].get("pause_requested")
    assert committed(state["billing"]) <= parse_budget("0.01")
    assert engine.store.calls(state["run"]["id"])[0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_restart_converts_durable_inflight_money_once_without_replay(make_swarm, tmp_path):
    provider = FakeProvider(gated=True)
    engine, state, _ = await make_swarm(provider)
    await single_worker(engine, state)
    await asyncio.wait_for(provider.entered.wait(), 2)
    expected = state["billing"]["reserved_nusd"]
    recovery_store = Store(tmp_path / "crash-snapshot.sqlite")
    engine.store.db.backup(recovery_store.db)
    # An independent database snapshot models a crash before response/settlement.
    for _ in range(2):
        recovered_provider = FakeProvider(gated=True)
        recovered = Engine(recovery_store, engine.settings, recovered_provider)
        try:
            await recovered.start()
            restored = recovered.state(state["run"]["id"])
            assert restored["run"]["status"] == "paused"
            assert not recovered.workers and not recovered_provider.calls
            assert restored["billing"]["reserved_nusd"] == 0
            assert restored["billing"]["uncertain_nusd"] == expected
            assert recovery_store.calls(state["run"]["id"])[0]["status"] == "uncertain"
        finally:
            await recovered.close()
    recovery_store.close()


@pytest.mark.asyncio
async def test_raising_budget_and_resuming_preserves_all_previous_charges(make_swarm):
    provider = FakeProvider(measured=usage(cached=0, output=8192))
    engine, state, client = await make_swarm(provider, budget="0.10")
    run_id = state["run"]["id"]
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 2)
    await engine.control(run_id, "pause")
    first_spent = state["billing"]["spent_nusd"]
    assert first_spent > parse_budget("0.01")
    prior_calls = deepcopy(engine.store.calls(run_id))
    limits = {"max_rounds": 2, "max_tokens": 100_000_000, "max_minutes": 1440, "max_output_tokens": 8192}
    lowered = await client.post(f"/api/runs/{run_id}/limits", json={**limits, "budget_usd": "0.01"})
    assert lowered.status_code == 400
    assert state["billing"]["budget_nusd"] == parse_budget("0.10")
    raised = await client.post(f"/api/runs/{run_id}/limits", json={**limits, "budget_usd": "2.00"})
    assert raised.status_code == 200, raised.text
    assert state["billing"]["budget_nusd"] == parse_budget("2.00")
    assert state["billing"]["spent_nusd"] == first_spent
    assert engine.store.calls(run_id) == prior_calls
    await engine.control(run_id, "resume")
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 2)
    assert state["billing"]["spent_nusd"] == first_spent * 2
    assert len(engine.store.calls(run_id)) == 2


@pytest.mark.asyncio
async def test_old_run_without_cost_ledger_and_unknown_models_fail_closed(make_swarm):
    provider = FakeProvider()
    engine, state, _ = await make_swarm(provider)
    state.pop("billing")
    state["usage"].update(requests=1, total_tokens=1250)
    state["run"]["status"] = "paused"
    engine.save(state)
    with pytest.raises(ValueError, match="pricing ledger"):
        await engine.control(state["run"]["id"], "resume")
    assert state["billing"]["legacy_unpriced"]
    assert not provider.calls
    with pytest.raises(ValueError, match="verified USD pricing"):
        await make_swarm(FakeProvider(), model="unknown-cheap-model")


@pytest.mark.asyncio
async def test_usage_exceeding_reservation_is_charged_and_stops_further_requests(make_swarm):
    provider = FakeProvider(measured=usage(cached=0, prompt=1_000_000, output=8192))
    engine, state, _ = await make_swarm(provider, budget="0.10", max_rounds=10)
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 2)
    record = engine.store.calls(state["run"]["id"])[0]
    assert record["cost_nusd"] > record["reserved_nusd"]
    assert state["billing"]["spent_nusd"] == cost_usage(provider.measured, pricing_for("deepseek-flash"))["cost_nusd"]
    assert len(provider.calls) == 1 and state["run"].get("pause_requested")


@pytest.mark.asyncio
async def test_failed_ledger_commit_cannot_persist_only_half_the_reservation(make_swarm):
    engine, state, _ = await make_swarm()
    original = engine.store.get(state["run"]["id"])
    changed = deepcopy(state)
    changed["billing"]["reserved_nusd"] = 12345
    with engine.store.db:
        engine.store.db.execute("""CREATE TRIGGER reject_test_call BEFORE INSERT ON api_calls
                                 BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        engine.store.record_call(changed, {"id": "uncommitted", "status": "reserved"})
    assert engine.store.get(state["run"]["id"]) == original
    assert engine.store.calls(state["run"]["id"]) == []


@pytest.mark.asyncio
async def test_worker_rolls_back_uncommitted_reservation_before_saving_error(make_swarm):
    provider = FakeProvider()
    engine, state, _ = await make_swarm(provider)
    run_id = state["run"]["id"]
    original_billing, original_usage = deepcopy(state["billing"]), deepcopy(state["usage"])
    with engine.store.db:
        engine.store.db.execute("""CREATE TRIGGER reject_reservation BEFORE INSERT ON api_calls
                                 BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")

    await engine._worker(state, PEERS[0])

    assert not provider.calls
    assert engine.store.calls(run_id) == []
    saved = engine.store.get(run_id)
    assert saved["billing"] == state["billing"] == original_billing
    assert saved["usage"] == state["usage"] == original_usage
    assert saved["agents"][0]["turns"] == 0
    assert saved["agents"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_failed_settlement_keeps_balances_consistent_for_restart(make_swarm):
    provider = FakeProvider()
    engine, state, _ = await make_swarm(provider)
    run_id = state["run"]["id"]
    with engine.store.db:
        engine.store.db.execute("""CREATE TRIGGER reject_settlement BEFORE INSERT ON api_calls
                                 WHEN NEW.status != 'reserved'
                                 BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")

    await engine._worker(state, PEERS[0])

    assert len(provider.calls) == 1
    record = engine.store.calls(run_id)[0]
    saved = engine.store.get(run_id)
    assert record["status"] == "reserved"
    assert saved["billing"] == state["billing"]
    assert saved["billing"]["spent_nusd"] == saved["billing"]["uncertain_nusd"] == 0
    assert saved["billing"]["reserved_nusd"] == record["reserved_nusd"] > 0
    assert saved["usage"]["total_tokens"] == saved["usage"]["uncertain_tokens"] == 0
    assert saved["usage"]["reserved_tokens"] == record["reserved_tokens"] > 0
    assert saved["agents"][0]["tokens"] == 0
    assert saved["billing"]["observed"]["calls"] == 0
    with engine.store.db:
        engine.store.db.execute("DROP TRIGGER reject_settlement")

    recovered_provider = FakeProvider()
    recovered = Engine(engine.store, engine.settings, recovered_provider)
    try:
        await recovered.start()
        restored = recovered.state(run_id)
        assert restored["run"]["status"] == "paused"
        assert not recovered_provider.calls
        assert restored["billing"]["reserved_nusd"] == restored["usage"]["reserved_tokens"] == 0
        assert restored["billing"]["spent_nusd"] == restored["usage"]["total_tokens"] == 0
        assert restored["billing"]["uncertain_nusd"] == record["reserved_nusd"]
        assert restored["usage"]["uncertain_tokens"] == record["reserved_tokens"]
        assert engine.store.calls(run_id)[0]["status"] == "uncertain"
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_token_limit_defaults_high_so_the_dollar_budget_binds_first(make_swarm):
    """The token cap previously stopped runs at a fraction of the money budget."""
    from swarm.app import NewRun
    assert NewRun(task="t").max_tokens == 1_000_000_000

    provider = FakeProvider(measured=usage(cached=0, prompt=200_000, output=8192))
    engine, state, _ = await make_swarm(provider, budget="0.10", max_rounds=10)
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 5)
    # The run stopped on money, not tokens, and says which limit to raise.
    assert state["run"]["pause_requested"].startswith("Dollar budget")
    assert state["usage"]["total_tokens"] < state["run"]["max_tokens"]


@pytest.mark.asyncio
async def test_hitting_the_token_limit_names_the_limit_to_raise(make_swarm):
    provider = FakeProvider()
    engine, state, _ = await make_swarm(provider, budget="100.00", max_rounds=10)
    state["run"]["max_tokens"] = 10_000
    state["usage"]["total_tokens"] = 9_999
    await asyncio.wait_for(engine._worker(state, PEERS[0]), 5)
    assert "Total tokens" in state["run"]["pause_requested"]
    assert not provider.calls
