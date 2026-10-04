from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import time
import uuid
from pathlib import Path

from .config import PEERS
from .billing import cost_usage, estimate_cost, estimate_tokens, new_observations, observe, parse_budget, pricing_for
from .provider import DeepSeek, ProviderError
from .store import now
from .toolbox import Toolbox, TOOL_SCHEMAS


def uid():
    return uuid.uuid4().hex[:16]


def schema(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}


S = {"type": "string"}
PEER_TOOLS = [
    schema("peer_message", "Send a concise A2A message to a peer or all peers; visible to the user.", {"to": S, "text": S}, ("to", "text")),
    schema("create_work", "Create a bounded shared work item; inspect board first to avoid duplicates.", {"title": S, "description": S}, ("title", "description")),
    schema("claim_work", "Atomically claim available work. Only its owner may finish or release it.", {"item_id": S}, ("item_id",)),
    schema("finish_work", "Submit your claimed work with evidence for independent peer review.", {"item_id": S, "result": S}, ("item_id", "result")),
    schema("review_work", "Independently verify another peer's submitted item; approve or reopen with evidence.", {"item_id": S, "approved": {"type": "boolean"}, "evidence": S}, ("item_id", "approved", "evidence")),
    schema("release_work", "Return your work to the available board if blocked.", {"item_id": S, "reason": S}, ("item_id", "reason")),
    schema("checkpoint", "Save factual durable memory: evidence, paths, results, next actions.", {"summary": S}, ("summary",)),
    schema("vote_complete", "Vote task complete against current board and user revision, citing evidence; requires all work reviewed.", {"evidence": S}, ("evidence",)),
    schema("wait_for_input", "Ask user a blocking question and sleep until new user input arrives.", {"question": S}, ("question",)),
    schema("yield_work", "Wait briefly for peers, avoiding redundant work or chatter.", {"seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, ("seconds",)),
]


class Engine:
    def __init__(self, store, settings, provider=None, notifier=None):
        self.store, self.settings = store, settings
        self.provider = provider or DeepSeek()
        self.notifier = notifier
        self.states = {s["run"]["id"]: s for s in store.all()}
        self.workers: dict[str, list[asyncio.Task]] = {}
        self.boxes = {}
        self.wakes = {}
        self.approval_waiters = {}
        self.monitor = None
        self.monitor_stop = asyncio.Event()
        self.persisted_at = {}
        self.public_url = "http://127.0.0.1:8767"

    async def start(self):
        for state in self.states.values():
            self.ensure_billing(state)
            for pending in self.store.calls(state["run"]["id"], status="reserved", limit=None):
                self.settle_call(state, self.agent(state, pending["peer"]), pending, None)
            if state["run"]["status"] in ("running", "waiting", "pausing"):
                state["run"]["status"] = "paused"
                state["usage"]["uncertain_tokens"] += state["usage"]["reserved_tokens"]
                state["usage"]["reserved_tokens"] = 0
                self._release_claims(state)
                for a in state["approvals"]:
                    if a["status"] == "pending":
                        a["status"] = "expired"
                for a in state["agents"]:
                    a["status"] = "paused"
                self.save(state)
                self.event(state, "recovered", data={"message": "Recovered after restart. Resume explicitly; uncertain actions are never replayed automatically."})
            self.save(state)
        self.monitor = asyncio.create_task(self._monitor())

    def ensure_billing(self, state):
        if "billing" in state:
            bill = state["billing"]
            bill.setdefault("observed", new_observations())
            if "cached_tokens" not in bill["observed"]:
                # Pre-off-peak runs learned an absolute input cost, which is no
                # longer valid once the hourly rate can change. The run's own
                # cache totals carry the same information, rate-free.
                bill["observed"].pop("input_cost_nusd", None)
                bill["observed"]["cached_tokens"] = bill.get("cached_tokens", 0)
                bill["observed"]["uncached_tokens"] = bill.get("uncached_tokens", 0)
            return bill
        legacy = state["usage"].get("requests", 0) > 0
        try:
            price = pricing_for(state["config"]["model"])
        except ValueError:
            price = None
        budget = parse_budget(state["run"].get("budget_usd", "1.00"))
        state["run"]["budget_usd"] = budget / 1000000000
        state["run"].setdefault("stall_minutes", 10)
        state["billing"] = {"version": 1, "pricing": price, "budget_nusd": budget, "spent_nusd": 0,
                            "reserved_nusd": 0, "uncertain_nusd": 0, "legacy_unpriced": legacy,
                            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                            "uncached_tokens": 0, "cache_unknown_requests": 0,
                            "observed": new_observations()}
        state.setdefault("progress_seconds", state["run"].get("active_seconds", 0))
        return state["billing"]

    def billing_summary(self, state):
        bill = self.ensure_billing(state)
        def usd(value):
            return f"{value // 1000000000}.{value % 1000000000:09d}"
        remaining = max(0, bill["budget_nusd"] - sum(bill[k] for k in ("spent_nusd", "reserved_nusd", "uncertain_nusd")))
        return {"budget_usd": usd(bill["budget_nusd"]), "spent_usd": usd(bill["spent_nusd"]),
                "reserved_usd": usd(bill["reserved_nusd"]), "uncertain_usd": usd(bill["uncertain_nusd"]),
                "remaining_usd": usd(remaining), "pricing": bill["pricing"], "legacy_unpriced": bill["legacy_unpriced"],
                **{k: bill[k] for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "uncached_tokens", "cache_unknown_requests")}}

    def settle_call(self, state, agent, call, measured):
        if call["status"] != "reserved":
            return
        bill, usage = state["billing"], state["usage"]
        # A failed transaction must leave the live state consistent with the
        # reserved ledger row; later error handling may save this state again.
        previous = [(value, deepcopy(value)) for value in (bill, usage, agent, call, state["run"])]
        count = measured.get("total_tokens") if isinstance(measured, dict) else None
        if isinstance(measured, dict) and ("prompt_tokens" in measured or "completion_tokens" in measured):
            prompt, completion = measured.get("prompt_tokens"), measured.get("completion_tokens")
            if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0 or prompt + completion != count:
                count = None
        usage["reserved_tokens"] -= call["reserved_tokens"]
        if type(count) is int and count >= 0:
            usage["total_tokens"] += count
            agent["tokens"] += count
        else:
            usage["uncertain_tokens"] += call["reserved_tokens"]
        bill["reserved_nusd"] -= call["reserved_nusd"]
        calculated = cost_usage(measured, call["pricing"], call["created_at"]) if isinstance(measured, dict) else None
        if calculated is None:
            # The provider returned no usable usage. Price it at this run's own
            # learned rate rather than holding the whole reservation forever.
            estimated = estimate_cost(call["input_bound_tokens"], call["max_output_tokens"],
                                      call["pricing"], bill.get("observed"), call["created_at"])
            bill["uncertain_nusd"] += estimated
            call.update(status="uncertain", settled_at=now(), cost_nusd=None, estimated_nusd=estimated)
        else:
            bill["spent_nusd"] += calculated["cost_nusd"]
            for field in ("prompt_tokens", "completion_tokens", "cached_tokens", "uncached_tokens"):
                bill[field] += calculated[field]
            bill["cache_unknown_requests"] += int(not calculated["cache_known"])
            call.update(status="settled", settled_at=now(), cost_nusd=calculated["cost_nusd"], tokens=calculated)
            observe(bill["observed"], call["input_bound_tokens"], calculated)
        # An estimate may land under the real cost, so the dollar cap is enforced
        # here on money actually owed rather than on any single call's forecast.
        if sum(bill[k] for k in ("spent_nusd", "uncertain_nusd")) >= bill["budget_nusd"]:
            state["run"]["pause_requested"] = "Dollar budget reached. Raise this task's budget in Run limits to continue."
        try:
            self.store.record_call(state, call)
        except Exception:
            for target, saved in previous:
                target.clear()
                target.update(saved)
            raise
        self.event(state, "api_cost", agent["id"], {"call_id": call["id"], "status": call["status"], "cost_nusd": call["cost_nusd"], "reserved_nusd": call["reserved_nusd"]})

    async def alert(self, state, kind, detail):
        clean = self.settings.redact(detail)[:1000]
        state["run"]["attention"] = clean
        self.save(state)
        self.event(state, "attention", data={"kind": kind, "message": clean})
        if self.notifier:
            await self.notifier.notify(kind, state["run"]["id"], clean)

    def progress(self, state):
        state["progress_seconds"] = state["run"]["active_seconds"]

    async def automatic_pause(self, state, reason):
        await self.control(state["run"]["id"], "pause")
        state["run"]["pause_reason"] = reason
        state["run"].pop("pause_requested", None)
        await self.alert(state, "blocked", reason)

    def save(self, state):
        if state.get("_saved_status") != state["run"]["status"]:
            state["run"]["status_updated_at"] = now()
            state["_saved_status"] = state["run"]["status"]
        self.store.save(state)
        self.persisted_at[state["run"]["id"]] = time.monotonic()

    def event(self, state, kind, agent_id=None, data=None):
        clean = self.settings.redact_value(json.loads(json.dumps(data or {}, default=str)))
        self.store.event(state["run"]["id"], kind, agent_id, clean)

    def summaries(self):
        return self.settings.redact_value([s["run"] for s in reversed(list(self.states.values()))])

    def state(self, run_id):
        if run_id not in self.states:
            raise KeyError(run_id)
        return self.states[run_id]

    def snapshot(self, run_id):
        s = self.state(run_id)
        return self.settings.redact_value({"run": s["run"], "agents": s["agents"], "items": s["items"], "messages": s["messages"][-300:], "approvals": s["approvals"][-100:], "events": self.store.events(run_id), "usage": s["usage"], "billing": self.billing_summary(s)})

    async def create(self, request):
        request = {**request, "task": self.settings.redact(request["task"])}
        if any(s["run"]["status"] in ("running", "waiting", "pausing") for s in self.states.values()):
            raise ValueError("Pause or stop the active swarm before starting another task")
        if not all(self.settings.keys) or len(set(self.settings.keys)) != 10:
            raise ValueError("Configure ten different DeepSeek API keys in Settings before starting a task")
        parse_budget(request.get("budget_usd", "1.00"))
        pricing_for(self.settings.values["model"])
        run_id = uid()
        workspace = Path(request.get("workspace") or self.settings.home / "workspaces" / run_id).expanduser().resolve()
        if workspace.is_relative_to(self.settings.home) and not workspace.is_relative_to(self.settings.home / "workspaces"):
            raise ValueError("Choose a workspace outside the harness credential and state directory")
        workspace.mkdir(parents=True, exist_ok=True)
        state = {
            "run": {**request, "id": run_id, "workspace": str(workspace), "status": "running", "created_at": now(), "active_seconds": 0, "max_output_tokens": self.settings.values.get("max_output_tokens", 8192)},
            "agents": [{"id": p, "name": f"Peer {i + 1:02}", "status": "ready", "turns": 0, "tokens": 0, "last_message": "", "memory": "", "vote": -1, "wait_revision": -1} for i, p in enumerate(PEERS)],
            "items": [], "messages": [], "approvals": [], "revision": 0, "user_revision": 0,
            "usage": {"total_tokens": 0, "reserved_tokens": 0, "uncertain_tokens": 0, "requests": 0},
            "config": dict(self.settings.values),
        }
        self.states[run_id] = state
        self.ensure_billing(state)
        self.save(state)
        self.message(state, "user", "all", request["task"])
        self.event(state, "created", data={"workspace": str(workspace)})
        self.launch(state)
        return state["run"]

    def message(self, state, sender, recipient, text):
        if recipient not in ["all", *PEERS]:
            raise ValueError("Unknown peer")
        text = self.settings.redact(str(text))[:30000]
        item = {"id": uid(), "from": sender, "to": recipient, "text": text, "created_at": now()}
        state["messages"].append(item)
        # Archive in the append-only event log; bound the hot state for long runs.
        state["messages"] = state["messages"][-1000:]
        if sender == "user":
            state["run"]["attention"] = ""
            self.progress(state)
            state["revision"] += 1
            state["user_revision"] += 1
            if state["run"]["status"] == "waiting":
                state["run"]["status"] = "running"
        self.save(state)
        self.event(state, "message", sender, item)
        for peer in PEERS if recipient == "all" else [recipient]:
            if peer != sender:
                self.wakes.setdefault((state["run"]["id"], peer), asyncio.Event()).set()
        return item

    def launch(self, state):
        run_id = state["run"]["id"]
        async def approve(peer, category, payload):
            return await self.approve(state, peer, category, payload)
        async def emit(*args, **kwargs):
            self.event(state, "tool_detail", data={"args": args, **kwargs})
        # Protect runtime secrets even if a broad parent folder is authorized.
        workspace = Path(state["run"]["workspace"])
        protected_paths = [] if workspace.is_relative_to(self.settings.home / "workspaces") else [self.settings.home]
        self.boxes[run_id] = Toolbox(str(workspace), state["run"]["permissions"], approve, emit, protected_paths=protected_paths)
        self.workers[run_id] = [asyncio.create_task(self._worker(state, p), name=f"{run_id}:{p}") for p in PEERS]

    def _release_claims(self, state):
        for item in state["items"]:
            if item["status"] == "claimed":
                item.update(status="open", owner=None)
                state["revision"] += 1

    async def control(self, run_id, action):
        state = self.state(run_id)
        if state["run"]["status"] == "pausing":
            raise ValueError("Workers are still stopping; wait for the pause to finish")
        if state["run"]["status"] in ("completed", "stopped") and action != "resume":
            raise ValueError("This task is terminal; create a new task to continue")
        if action == "resume":
            if state["run"]["status"] in ("completed", "stopped"):
                raise ValueError("Create a new task to continue a completed or stopped run")
            if state["run"]["status"] in ("running", "waiting"):
                return state["run"]
            if any(s["run"]["id"] != run_id and s["run"]["status"] in ("running", "waiting", "pausing") for s in self.states.values()):
                raise ValueError("Another swarm is active; pause it first")
            usage = state["usage"]
            bill = self.ensure_billing(state)
            if bill["legacy_unpriced"] or bill["pricing"] is None:
                raise ValueError("This run has no complete pricing ledger. Start a new task with a supported priced model and an explicit USD budget.")
            if sum(bill[k] for k in ("spent_nusd", "reserved_nusd", "uncertain_nusd")) >= bill["budget_nusd"]:
                raise ValueError("Dollar budget exhausted or held for uncertain requests. Raise this run's budget to continue.")
            if usage["total_tokens"] + usage["reserved_tokens"] + usage["uncertain_tokens"] >= state["run"]["max_tokens"]:
                raise ValueError("Token budget exhausted. Increase this run's limits before resuming.")
            if state["run"]["active_seconds"] >= state["run"]["max_minutes"] * 60:
                raise ValueError("Time budget exhausted. Increase this run's limits before resuming.")
            if all(a["turns"] >= state["run"]["max_rounds"] for a in state["agents"]):
                raise ValueError("Turn budget exhausted. Increase this run's limits before resuming.")
            if not all(self.settings.keys):
                raise ValueError("Configure all ten keys first")
            state["run"]["status"] = "running"
            state["run"].update(pause_reason="", attention="")
            state["run"].pop("pause_requested", None)
            self.progress(state)
            for agent in state["agents"]:
                agent["wait_revision"] = -1
                agent["status"] = "ready"
            self.save(state)
            self.launch(state)
        else:
            target_status = "paused" if action == "pause" else "stopped"
            state["run"]["status"] = "pausing"
            self.save(state)
            tasks = self.workers.pop(run_id, [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            box = self.boxes.pop(run_id, None)
            if box:
                await box.close()
            self._release_claims(state)
            state["run"]["status"] = target_status
            for agent in state["agents"]:
                agent["status"] = state["run"]["status"]
            for approval in state["approvals"]:
                if approval["status"] == "pending":
                    approval["status"] = "expired"
            self.save(state)
        self.event(state, "control", data={"action": action})
        return state["run"]

    async def set_permissions(self, run_id, permissions):
        state = self.state(run_id)
        if state["run"]["status"] == "pausing":
            raise ValueError("Wait for workers to finish stopping before changing access")
        active = state["run"]["status"] in ("running", "waiting")
        if active:
            await self.control(run_id, "pause")
        state["run"]["permissions"].update(permissions)
        self.save(state)
        self.event(state, "permissions", data=permissions)
        if active:
            await self.control(run_id, "resume")
        return state["run"]

    async def approve(self, state, peer, category, payload):
        item = {"id": uid(), "agent_id": peer, "category": category, "payload": payload, "status": "pending", "created_at": now()}
        state["approvals"].append(item)
        self.save(state)
        self.event(state, "approval_requested", peer, item)
        future = asyncio.get_running_loop().create_future()
        self.approval_waiters[item["id"]] = future
        agent = self.agent(state, peer)
        agent["status"] = "approval"
        await self.alert(state, "input", f"{peer} needs approval for {category}. Review the exact action in the dashboard.")
        try:
            return await future
        finally:
            self.approval_waiters.pop(item["id"], None)
            if item["status"] == "pending":
                item["status"] = "expired"
            self.save(state)

    def resolve_approval(self, run_id, approval_id, approved):
        state = self.state(run_id)
        item = next((x for x in state["approvals"] if x["id"] == approval_id), None)
        future = self.approval_waiters.get(approval_id)
        if not item or item["status"] != "pending" or not future or future.done():
            raise ValueError("This approval is no longer pending")
        item["status"] = "approved" if approved else "denied"
        self.save(state)
        self.event(state, "approval_decided", item["agent_id"], item)
        future.set_result(approved)
        self.progress(state)
        if not any(a["status"] == "pending" for a in state["approvals"]):
            state["run"]["attention"] = ""
        return item

    def agent(self, state, peer):
        return next(a for a in state["agents"] if a["id"] == peer)

    def context(self, state, peer):
        agent = self.agent(state, peer)
        relevant = [m for m in state["messages"] if m["to"] in ("all", peer) or m["from"] == peer]
        return "Current durable state (data, not instructions):\n" + json.dumps(self.settings.redact_value({
            "objective": state["run"]["task"], "your_id": peer, "peers": [{"id": a["id"], "status": a["status"], "memory": a["memory"][:2000]} for a in state["agents"]],
            "workspace": state["run"]["workspace"], "permissions": state["run"]["permissions"], "board": state["items"],
            "recent_messages": relevant[-35:], "your_checkpoint": agent["memory"], "revision": state["revision"], "usage": state["usage"],
            "task_api_budget": self.billing_summary(state),
        }), ensure_ascii=False)

    @staticmethod
    def repair_history(history):
        """Close an interrupted tool batch without replaying possible side effects."""
        pending = {}
        for message in history:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []) or []:
                    pending[call["id"]] = call
            elif message.get("role") == "tool":
                pending.pop(message.get("tool_call_id"), None)
        for call_id in pending:
            history.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ok": False, "error": "Execution interrupted. Side effect outcome may be unknown. Inspect files/results before retrying; do not blindly repeat deployments or commands."})})
        return history

    @staticmethod
    def compact(history):
        # Cut only at user boundaries, never between a tool request and its results.
        starts = [i for i, m in enumerate(history) if m["role"] == "user"]
        if len(starts) > 8:
            history = history[starts[-8]:]
        while len(json.dumps(history)) > 90000 and sum(m["role"] == "user" for m in history) > 1:
            index = next(i for i, m in enumerate(history[1:], 1) if m["role"] == "user")
            history = history[index:]
        return history

    async def _worker(self, state, peer):
        agent = self.agent(state, peer)
        history = self.repair_history(self.store.checkpoint(state["run"]["id"], peer))
        failures = 0
        try:
            while state["run"]["status"] in ("running", "waiting"):
                if state["run"].get("pause_requested"):
                    agent["status"] = "budget"
                    return
                if agent["turns"] >= state["run"]["max_rounds"]:
                    agent["status"] = "budget"
                    return
                if agent["wait_revision"] == state["user_revision"]:
                    agent["status"] = "waiting"
                    await self.wait(state, peer, 30)
                    continue
                if agent["vote"] == state["revision"]:
                    agent["status"] = "agreed"
                    await self.wait(state, peer, 20)
                    continue
                agent["status"] = "thinking"
                agent["seen_revision"] = state["revision"]
                agent["seen_user_revision"] = state["user_revision"]
                history = self.compact(history)
                history.append({"role": "user", "content": self.context(state, peer)})
                prompt = self.settings.redact(state["config"]["system_prompt"]).replace("{agent_id}", peer)
                messages = [{"role": "system", "content": prompt}, *history]
                schemas = [*PEER_TOOLS, *TOOL_SCHEMAS]
                # The UTF-8 byte bound includes the schemas, plus framing overhead.
                # No cache discount is assumed before the provider reports usage.
                input_bound = len(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False).encode("utf-8")) + 4096
                output_bound = state["config"].get("max_output_tokens", 8192)
                usage = state["usage"]
                bill = self.ensure_billing(state)
                reservation = estimate_tokens(input_bound, output_bound, bill.get("observed"))
                if bill["legacy_unpriced"] or bill["pricing"] is None:
                    raise ValueError("No trusted price ledger for this run; start a new task with a supported model")
                money_reservation = estimate_cost(input_bound, output_bound, bill["pricing"], bill.get("observed"))
                available = bill["budget_nusd"] - sum(bill[k] for k in ("spent_nusd", "reserved_nusd", "uncertain_nusd"))
                if money_reservation > available:
                    history.pop()  # No request was sent; do not accumulate duplicate context.
                    if bill["reserved_nusd"] > 0:
                        agent["status"] = "budget_wait"
                        await asyncio.sleep(0.5)
                        continue
                    agent["status"] = "budget"
                    state["run"]["pause_requested"] = "Dollar budget cannot cover another bounded API request. Raise the task budget in Run limits to continue."
                    self.save(state)
                    return
                if sum(usage[k] for k in ("total_tokens", "reserved_tokens", "uncertain_tokens")) + reservation > state["run"]["max_tokens"]:
                    agent["status"] = "budget"
                    state["run"]["pause_requested"] = "Token limit reached. Raise 'Total tokens' in Run limits to continue; the dollar budget still has room."
                    self.event(state, "budget", peer, {"message": "Insufficient remaining token budget for a bounded request"})
                    self.save(state)
                    return
                usage["reserved_tokens"] += reservation
                bill["reserved_nusd"] += money_reservation
                usage["requests"] += 1
                agent["turns"] += 1
                request_record = {"id": uid(), "peer": peer, "status": "reserved", "created_at": now(),
                                  "reserved_tokens": reservation, "input_bound_tokens": input_bound, "max_output_tokens": output_bound,
                                  "reserved_nusd": money_reservation, "pricing": dict(bill["pricing"])}
                # No await between the shared balance check and its durable reservation.
                try:
                    self.store.record_call(state, request_record)
                except Exception:
                    # The request was never sent or committed. Undo the live
                    # counters before the worker's error handler persists them.
                    usage["reserved_tokens"] -= reservation
                    bill["reserved_nusd"] -= money_reservation
                    usage["requests"] -= 1
                    agent["turns"] -= 1
                    raise
                try:
                    response, measured = await self.provider.complete(self.settings.keys[PEERS.index(peer)], dict(state["config"]), messages, schemas)
                    self.settle_call(state, agent, request_record, measured)
                    failures = 0
                except ProviderError as error:
                    self.settle_call(state, agent, request_record, getattr(error, "usage", None))
                    failures += 1
                    agent["status"] = "retrying" if error.retryable and failures < 5 else "error"
                    agent["last_message"] = self.settings.redact(str(error))
                    self.event(state, "provider_error", peer, {"message": str(error), "attempt": failures})
                    if not error.retryable or failures >= 5:
                        await self.alert(state, "blocked", f"{peer} stopped after a provider error: {error}")
                        return
                    await asyncio.sleep(min(60, 2 ** failures + PEERS.index(peer) / 10))
                    continue
                finally:
                    if request_record["status"] == "reserved":
                        self.settle_call(state, agent, request_record, None)
                    self.save(state)
                if state["run"].get("pause_requested"):
                    agent["status"] = "budget"
                    return
                history.append(response)
                self.store.checkpoint(state["run"]["id"], peer, history)
                content = response.get("content")
                if content:
                    agent["last_message"] = self.settings.redact(content)[:4000]
                    self.message(state, peer, "all", content)
                for call in response.get("tool_calls", []) or []:
                    agent["status"] = "working"
                    name = call.get("function", {}).get("name", "")
                    agent["active_tool"] = name
                    try:
                        args = json.loads(call["function"]["arguments"])
                        if not isinstance(args, dict):
                            raise ValueError("Tool arguments must be an object")
                        self.event(state, "tool_started", peer, {"name": name, "arguments": args})
                        result = await self.execute(state, peer, name, args)
                    except (ValueError, KeyError, TypeError) as error:
                        result = {"ok": False, "error": str(error)}
                    finally:
                        agent.pop("active_tool", None)
                    if isinstance(result, dict) and result.get("ok") is True and name not in {"checkpoint", "yield_work", "peer_message", "wait_for_input", "claim_work", "release_work"}:
                        signature = hashlib.sha256(json.dumps([name, args, result], default=str, sort_keys=True).encode()).hexdigest()
                        seen = state.setdefault("progress_fingerprints", [])
                        if signature not in seen:
                            seen.append(signature)
                            state["progress_fingerprints"] = seen[-200:]
                            self.progress(state)
                    serialized = json.dumps(self.settings.redact_value(result), default=str)[:30000]
                    history.append({"role": "tool", "tool_call_id": call["id"], "content": serialized})
                    self.store.checkpoint(state["run"]["id"], peer, history)
                    self.event(state, "tool_result", peer, {"name": name, "result": serialized[:10000]})
                    self.save(state)
                agent["status"] = "ready"
                if not response.get("tool_calls"):
                    await self.wait(state, peer, 6)
                else:
                    await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            agent["status"] = "error"
            agent["last_message"] = self.settings.redact(str(error))[:1000]
            self.event(state, "worker_error", peer, {"type": type(error).__name__, "message": agent["last_message"]})
            await self.alert(state, "blocked", f"{peer} stopped unexpectedly: {agent['last_message']}")
        finally:
            if agent["status"] in ("error", "budget"):
                for item in state["items"]:
                    if item["status"] == "claimed" and item["owner"] == peer:
                        item.update(status="open", owner=None)
                        state["revision"] += 1
            self.save(state)

    async def wait(self, state, peer, seconds):
        wake = self.wakes.setdefault((state["run"]["id"], peer), asyncio.Event())
        try:
            await asyncio.wait_for(wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            wake.clear()

    async def execute(self, state, peer, name, args):
        agent = self.agent(state, peer)
        if name == "peer_message":
            from .a2a import dispatch_a2a, peer_message
            engine = self
            class LocalPeer:
                async def a2a_send(self, target, params):
                    from .a2a import parse_text
                    engine.message(state, peer, target or "all", parse_text(params))
                    return {"task": engine.a2a_task(state)}
            destination = args["to"]
            if destination not in ["all", *PEERS]:
                raise ValueError("Unknown peer")
            return await dispatch_a2a(LocalPeer(), peer_message(peer, destination, state["run"]["id"], args["text"]), None if destination == "all" else destination)
        if name == "create_work":
            title = self.settings.redact(str(args["title"]).strip())[:200]
            if not title or len(state["items"]) >= 200:
                raise ValueError("Provide a title; at most 200 items per run")
            existing = next((i for i in state["items"] if i["title"].casefold() == title.casefold()), None)
            if existing:
                return {"ok": True, "item": existing, "existing": True}
            item = {"id": uid(), "title": title, "description": self.settings.redact(str(args["description"]))[:8000], "status": "open", "owner": None, "result": "", "reviewer": None}
            state["items"].append(item)
            state["revision"] += 1
            self.save(state)
            return {"ok": True, "item": item}
        if name in ("claim_work", "finish_work", "review_work", "release_work"):
            item = next((i for i in state["items"] if i["id"] == args["item_id"]), None)
            if not item:
                raise ValueError("Unknown work item")
            if name == "claim_work":
                if item["status"] != "open":
                    raise ValueError("Already claimed or finished; choose other work")
                item.update(status="claimed", owner=peer)
            elif name == "finish_work":
                if item["status"] != "claimed" or item["owner"] != peer:
                    raise ValueError("Only the current owner can finish this item")
                if not str(args["result"]).strip():
                    raise ValueError("Provide result evidence")
                item.update(status="review", result=self.settings.redact(str(args["result"]))[:12000])
            elif name == "review_work":
                if item["status"] != "review" or item["owner"] == peer:
                    raise ValueError("Review must be performed by a different peer on submitted work")
                if not str(args["evidence"]).strip():
                    raise ValueError("Provide verification evidence")
                item.update(status="done" if args["approved"] is True else "open", reviewer=peer, review=self.settings.redact(str(args["evidence"]))[:8000])
                if item["status"] == "open":
                    item["owner"] = None
            else:
                if item["status"] != "claimed" or item["owner"] != peer:
                    raise ValueError("Only the owner may release a claim")
                item.update(status="open", owner=None)
            state["revision"] += 1
            self.save(state)
            self.event(state, "board", peer, item)
            return {"ok": True, "item": item}
        if name == "checkpoint":
            agent["memory"] = self.settings.redact(str(args["summary"]))[:12000]
            self.save(state)
            return {"ok": True}
        if name == "vote_complete":
            if agent.get("seen_revision", -1) != state["revision"]:
                raise ValueError("The task or work board changed since your last observation. Inspect the updated state before voting.")
            if not state["items"] or any(i["status"] != "done" for i in state["items"]):
                raise ValueError("All shared work must be independently reviewed before completion")
            if not str(args["evidence"]).strip():
                raise ValueError("Cite completion evidence")
            agent["vote"] = state["revision"]
            self.event(state, "completion_vote", peer, {"evidence": args["evidence"]})
            self.save(state)
            return {"ok": True, "votes": sum(a["vote"] == state["revision"] for a in state["agents"]), "required": 10}
        if name == "wait_for_input":
            if agent.get("seen_user_revision", -1) != state["user_revision"]:
                return {"ok": False, "error": "New user input arrived during your response. Read it before asking or waiting again."}
            self.message(state, peer, "all", str(args["question"]))
            agent["wait_revision"] = state["user_revision"]
            await self.alert(state, "input", f"{peer} needs your input: {args['question']}")
            return {"ok": True, "waiting": True}
        if name == "yield_work":
            await self.wait(state, peer, max(1, min(60, int(args.get("seconds", 10)))))
            return {"ok": True}
        return await self.boxes[state["run"]["id"]].execute(name, args, peer)

    async def _monitor(self):
        previous = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self.monitor_stop.wait(), timeout=1)
                return
            except asyncio.TimeoutError:
                pass
            tick = time.monotonic()
            delta, previous = tick - previous, tick
            for run_id, state in list(self.states.items()):
                if state["run"]["status"] not in ("running", "waiting"):
                    continue
                state["run"]["active_seconds"] += delta
                if state["run"].get("pause_requested"):
                    await self.automatic_pause(state, state["run"]["pause_requested"])
                elif state["run"]["active_seconds"] >= state["run"]["max_minutes"] * 60:
                    await self.automatic_pause(state, "Active time limit reached. Adjust Run limits before resuming.")
                    self.event(state, "budget", data={"message": "Active time limit reached"})
                elif all(a["vote"] == state["revision"] and a["status"] not in ("working", "thinking", "approval") for a in state["agents"]) and state["items"] and all(i["status"] == "done" for i in state["items"]):
                    await self.control(run_id, "pause")
                    state["run"]["status"] = "completed"
                    for a in state["agents"]:
                        a["status"] = "completed"
                    self.message(state, "swarm", "all", "All ten peers agreed the reviewed work is complete. Results are on the work board.")
                    await self.alert(state, "completion", "Task complete: all ten peers agreed the reviewed work is finished.")
                elif self.workers.get(run_id) and all(t.done() for t in self.workers[run_id]):
                    await self.automatic_pause(state, "No workers remain active. Inspect errors and budgets before resuming.")
                    self.event(state, "paused", data={"message": "No workers remain active; inspect errors and budget events before resuming"})
                elif all(a["status"] in ("waiting", "agreed", "error", "budget") for a in state["agents"]):
                    if any(a["status"] in ("error", "budget") for a in state["agents"]):
                        await self.automatic_pause(state, "The swarm cannot continue with the remaining peers. Review errors or limits, then Resume.")
                    elif state["run"]["status"] != "waiting":
                        state["run"]["status"] = "waiting"
                        await self.alert(state, "input", "The swarm is waiting for your input. Read the conversation to continue.")
                elif (state["run"]["active_seconds"] - state.get("progress_seconds", 0) >= state["run"].get("stall_minutes", 10) * 60
                      and not any(a.get("active_tool") in ("run_command", "deploy_command") or a["status"] == "approval" for a in state["agents"])):
                    await self.automatic_pause(state, "No new verifiable progress was recorded within the stall limit. Review the work, provide direction, or adjust the limit and Resume.")
                if state.get("_saved_status") != state["run"]["status"] or tick - self.persisted_at.get(run_id, 0) >= 15:
                    self.save(state)

    def a2a_task(self, state):
        mapping = {"running": "TASK_STATE_WORKING", "waiting": "TASK_STATE_INPUT_REQUIRED", "paused": "TASK_STATE_INPUT_REQUIRED", "completed": "TASK_STATE_COMPLETED", "stopped": "TASK_STATE_CANCELED"}
        run = state["run"]
        return self.settings.redact_value({
            "id": run["id"], "contextId": run["id"],
            "status": {"state": mapping.get(run["status"], "TASK_STATE_UNSPECIFIED"), "timestamp": run.get("status_updated_at", run["created_at"])},
            "artifacts": [{"artifactId": i["id"], "name": i["title"], "parts": [{"text": i["result"]}]} for i in state["items"] if i["status"] == "done"],
            "history": [{
                "messageId": message.get("a2a_message_id", message["id"]),
                "contextId": run["id"], "taskId": run["id"],
                "role": "ROLE_USER" if message["from"] in ("user", "external") else "ROLE_AGENT",
                "parts": [{"text": message["text"]}],
            } for message in state["messages"]],
        })

    async def a2a_send(self, agent_id, params):
        from .a2a import A2AError, parse_text
        text = parse_text(params)
        message = params["message"]
        run_id = message.get("taskId") or message.get("contextId")
        if not run_id:
            raise A2AError(-32602, "Create a task in the local dashboard first, then supply its taskId or contextId", "message.taskId")
        state = self.state(run_id)
        if message.get("contextId") and message["contextId"] != state["run"]["id"]:
            raise A2AError(-32602, "contextId must match the task's context", "message.contextId")
        if state["run"]["status"] in ("completed", "stopped"):
            raise A2AError(-32004, "Task is terminal; create a new task in the dashboard")
        if any(existing.get("a2a_message_id") == message["messageId"] for existing in state["messages"]):
            return {"task": self.a2a_task(state)}
        # Authentication grants this caller the owner's conversation channel,
        # never a claimed peer identity supplied in untrusted message metadata.
        state["revision"] += 1
        state["user_revision"] += 1
        state["run"]["attention"] = ""
        self.progress(state)
        if state["run"]["status"] == "waiting":
            state["run"]["status"] = "running"
        stored = self.message(state, "external", agent_id or "all", text)
        stored["a2a_message_id"] = message["messageId"]
        self.save(state)
        return {"task": self.a2a_task(state)}

    async def a2a_get(self, params):
        return self.a2a_task(self.state(params["id"]))

    async def a2a_cancel(self, params):
        from .a2a import A2AError
        state = self.state(params["id"])
        if state["run"]["status"] in ("completed", "stopped"):
            raise A2AError(-32002, "Task is already terminal and cannot be canceled")
        await self.control(params["id"], "stop")
        return self.a2a_task(state)

    async def a2a_list(self, params):
        import base64
        from datetime import datetime
        from .a2a import A2AError

        def sort_key(task):
            return datetime.fromisoformat(task["status"]["timestamp"].replace("Z", "+00:00")), task["id"]

        tasks = [self.a2a_task(state) for state in self.states.values()]
        if params.get("contextId"):
            tasks = [task for task in tasks if task["contextId"] == params["contextId"]]
        if params.get("status"):
            tasks = [task for task in tasks if task["status"]["state"] == params["status"]]
        if params.get("statusTimestampAfter"):
            after = datetime.fromisoformat(params["statusTimestampAfter"].replace("Z", "+00:00"))
            tasks = [task for task in tasks if sort_key(task)[0] >= after]
        tasks.sort(key=sort_key, reverse=True)
        total = len(tasks)
        size = min(int(params.get("pageSize", 50)), 100)
        if params.get("pageToken"):
            try:
                cursor = json.loads(base64.urlsafe_b64decode(params["pageToken"].encode("ascii")))
                if not isinstance(cursor, list) or len(cursor) != 2 or not all(isinstance(value, str) for value in cursor):
                    raise ValueError()
                stamp = datetime.fromisoformat(cursor[0].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError()
                tasks = [task for task in tasks if sort_key(task) < (stamp, cursor[1])]
            except (ValueError, TypeError, UnicodeError):
                raise A2AError(-32602, "Invalid pageToken", "pageToken") from None
        page = tasks[:size]
        token = ""
        if len(tasks) > size:
            last = page[-1]
            token = base64.urlsafe_b64encode(json.dumps([last["status"]["timestamp"], last["id"]]).encode()).decode()
        return {"tasks": page, "nextPageToken": token, "pageSize": size, "totalSize": total}

    async def close(self):
        if self.monitor:
            self.monitor_stop.set()
            await asyncio.gather(self.monitor, return_exceptions=True)
        for run_id in list(self.workers):
            state = self.states[run_id]
            if state["run"]["status"] in ("running", "waiting"):
                await self.control(run_id, "pause")
        await self.provider.close()
