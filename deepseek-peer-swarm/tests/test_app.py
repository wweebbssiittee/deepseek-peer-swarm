import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from swarm.app import create_app
from swarm.config import Settings
from swarm.notifications import Notifier


TEST_KEYS = [f"sk-test-only-distinct-slot-{i:02}-secret" for i in range(10)]


class WaitingProvider:
    def __init__(self):
        self.command_next = False
        self.calls = []
        self.closed = False

    async def complete(self, key, config, messages, tools):
        self.calls.append(key)
        if self.command_next:
            self.command_next = False
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "approval-test-call", "type": "function", "function": {
                    "name": "run_command", "arguments": json.dumps({
                        "command": "Write-Output 'must await approval'" if os.name == "nt" else "printf 'must await approval'",
                        "timeout": 10,
                    }),
                },
            }]}, {"total_tokens": 100}
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {f"DEEPSEEK_API_KEY_{i}": "" for i in range(1, 11)})
        self.env.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.home = self.parent / "private-runtime"
        self.provider = WaitingProvider()
        self.notifier = Notifier(self.home, play=lambda _: None)
        self.app = create_app(self.home, provider=self.provider, notifier=self.notifier)
        self.engine = self.app.state.engine
        self.settings = self.app.state.settings
        self.settings.keys = TEST_KEYS.copy()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8767")
        bootstrap = (await self.client.get("/api/bootstrap")).json()
        self.headers = {"X-Swarm-Token": bootstrap["csrf_token"], "Origin": "http://127.0.0.1:8767"}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()
        self.env.stop()

    async def new_run(self, **overrides):
        payload = {"task": "A bounded verification task", **overrides}
        response = await self.client.post("/api/runs", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def pause(self, run_id):
        response = await self.client.post(f"/api/runs/{run_id}/control", json={"action": "pause"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_local_origin_token_and_host_checks(self):
        missing = await self.client.post("/api/settings", json={"thinking": False})
        self.assertEqual(missing.status_code, 403)
        wrong = await self.client.post("/api/settings", json={"thinking": False}, headers={"X-Swarm-Token": "incorrect"})
        self.assertEqual(wrong.status_code, 403)
        hostile = await self.client.post("/api/settings", json={"thinking": False}, headers={**self.headers, "Origin": "https://attacker.example"})
        self.assertEqual(hostile.status_code, 403)
        cross_site = await self.client.get("/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross_site.status_code, 403)
        foreign_host = await self.client.get("/api/bootstrap", headers={"Host": "attacker.example"})
        self.assertEqual(foreign_host.status_code, 400)
        good = await self.client.post("/api/settings", json={"thinking": False}, headers={"Authorization": f"Bearer {self.settings.token}", "Origin": "http://localhost:8767"})
        self.assertEqual(good.status_code, 200)
        self.assertFalse(good.json()["thinking"])

    async def test_invalid_key_payload_never_echoes_submitted_secrets(self):
        secret = "sk-unsaved-secret-must-never-echo"
        for payload in [
            {"keys": [secret]},
            {"keys": [secret] * 11},
            {"keys": [{"secret": secret}] * 10},
            {"keys": [secret] + [""] * 9, "unexpected": secret},
        ]:
            response = await self.client.post("/api/settings", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
            self.assertNotIn('"input"', response.text)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows account-bound")
    async def test_keys_are_dpapi_encrypted_and_never_returned(self):
        # Start blank so the save is a real change and the vault is written.
        self.settings.keys = [""] * 10
        response = await self.client.post("/api/settings", json={"keys": TEST_KEYS}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["configured_keys"], [True] * 10)
        encrypted = (self.home / "keys.enc").read_bytes()
        for key in TEST_KEYS:
            self.assertNotIn(key, response.text)
            self.assertNotIn(key.encode(), encrypted)
        restored = Settings(self.home)
        self.assertEqual(restored.keys, TEST_KEYS)
        preserving = await self.client.post("/api/settings", json={"keys": [""] * 10}, headers=self.headers)
        self.assertEqual(preserving.status_code, 200)
        self.assertEqual(self.settings.keys, TEST_KEYS)
        public = (await self.client.get("/api/bootstrap")).text
        for key in TEST_KEYS:
            self.assertNotIn(key, public)

    async def test_initial_task_and_messages_redact_configured_keys(self):
        self.settings.keys = TEST_KEYS.copy()
        run = await self.new_run(task="Inspect this credential safely: " + TEST_KEYS[0])
        await self.pause(run["id"])
        message = await self.client.post(f"/api/runs/{run['id']}/messages", json={"text": TEST_KEYS[1]}, headers=self.headers)
        self.assertEqual(message.status_code, 200)
        self.assertEqual(message.json()["text"], "[REDACTED]")
        snapshots = [
            json.dumps(run),
            (await self.client.get(f"/api/runs/{run['id']}")).text,
            (await self.client.get(f"/api/runs/{run['id']}/export")).text,
            (await self.client.get("/api/bootstrap")).text,
            json.dumps(self.engine.store.get(run["id"])),
        ]
        for snapshot in snapshots:
            for key in TEST_KEYS:
                self.assertNotIn(key, snapshot)

    async def test_broad_workspace_cannot_touch_runtime_or_sidecars(self):
        run = await self.new_run(workspace=str(self.parent), permissions={"read_files": True, "write_files": True,
                                  "internet": False, "commands": "deny", "deploy": "deny"})
        box = self.engine.boxes[run["id"]]
        # Includes currently absent temporary/sidecar names to protect future files.
        for name in ["keys.enc", "keys.enc.tmp", "access-token.txt", "swarm.sqlite",
                     "swarm.sqlite-wal", "swarm.sqlite-shm", "settings.tmp", "server.log"]:
            relative = f"private-runtime/{name}"
            result = await box.execute("write_file", {"path": relative, "content": "overwrite"}, "peer-01")
            self.assertFalse(result["ok"], relative)
            self.assertIn("protected", result["error"].lower(), relative)
        listed = await box.execute("list_files", {}, "peer-01")
        self.assertNotIn("private-runtime", [entry["name"] for entry in listed["entries"]])
        secret = await box.execute("write_file", {"path": ".env", "content": "secret"}, "peer-01")
        self.assertFalse(secret["ok"])
        ordinary = await box.execute("write_file", {"path": "result.txt", "content": "safe"}, "peer-01")
        self.assertTrue(ordinary["ok"])
        await self.pause(run["id"])

    async def test_default_workspace_allows_authorized_file_tools(self):
        run = await self.new_run(permissions={"read_files": True, "write_files": True, "internet": False,
                                            "commands": "deny", "deploy": "deny"})
        box = self.engine.boxes[run["id"]]
        result = await box.execute("write_file", {"path": "result.txt", "content": "safe"}, "peer-01")
        self.assertTrue(result["ok"], result)
        await self.pause(run["id"])

    async def test_a_run_is_refused_without_ten_distinct_keys(self):
        for keys in ([""] * 10, ["sk-same"] * 10, TEST_KEYS[:9] + [""]):
            self.settings.keys = list(keys)
            response = await self.client.post("/api/runs", json={"task": "test"}, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("ten different DeepSeek API keys", response.text)

    async def test_runtime_subdirectory_cannot_masquerade_as_workspace(self):
        response = await self.client.post("/api/runs", json={"task": "test",
                                         "workspace": str(self.home / "logs" / "workspaces")}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    async def test_pause_cancels_pending_approval_and_releases_claims(self):
        self.settings.keys = TEST_KEYS.copy()
        self.provider.command_next = True
        run = await self.new_run()
        state = self.engine.state(run["id"])
        async with asyncio.timeout(5):
            while not state["approvals"]:
                await asyncio.sleep(0.01)
        approval_id = state["approvals"][0]["id"]
        created = await self.engine.execute(state, "peer-02", "create_work", {"title": "Claimed task", "description": "Release on pause"})
        await self.engine.execute(state, "peer-02", "claim_work", {"item_id": created["item"]["id"]})
        workers = self.engine.workers[run["id"]].copy()
        paused = await self.pause(run["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(all(task.done() for task in workers))
        self.assertNotIn(run["id"], self.engine.workers)
        self.assertNotIn(run["id"], self.engine.boxes)
        self.assertNotIn(approval_id, self.engine.approval_waiters)
        self.assertEqual(state["approvals"][0]["status"], "expired")
        self.assertEqual(created["item"]["status"], "open")
        self.assertIsNone(created["item"]["owner"])
        expired = await self.client.post(f"/api/runs/{run['id']}/approvals/{approval_id}", json={"approved": True}, headers=self.headers)
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(state["usage"]["reserved_tokens"], 0)
        self.assertGreater(state["usage"]["uncertain_tokens"], 0)

    async def test_permission_change_stops_old_workers_and_applies_revocation(self):
        run = await self.new_run(permissions={"read_files": True, "write_files": True, "internet": True,
                                            "commands": "allow", "deploy": "allow"})
        old_box = self.engine.boxes[run["id"]]
        old_workers = self.engine.workers[run["id"]].copy()
        response = await self.client.post(f"/api/runs/{run['id']}/permissions", headers=self.headers, json={
            "read_files": False, "write_files": False, "internet": False, "commands": "deny", "deploy": "deny",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "running")
        self.assertTrue(old_box._closed)
        self.assertTrue(all(task.done() for task in old_workers))
        new_box = self.engine.boxes[run["id"]]
        self.assertIsNot(new_box, old_box)
        denied = await new_box.execute("write_file", {"path": "forbidden.txt", "content": "no"}, "peer-01")
        self.assertFalse(denied["ok"])
        self.assertFalse((Path(run["workspace"]) / "forbidden.txt").exists())
        await self.pause(run["id"])

    async def test_resume_create_and_settings_are_blocked_until_pause_finishes(self):
        run = await self.new_run()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def delayed_cleanup():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await allow_cleanup.wait()
                raise

        worker = asyncio.create_task(delayed_cleanup())
        self.engine.workers[run["id"]].append(worker)
        await asyncio.sleep(0)
        pause_request = asyncio.create_task(self.client.post(f"/api/runs/{run['id']}/control", json={"action": "pause"}, headers=self.headers))
        try:
            await asyncio.wait_for(cleanup_started.wait(), 5)
            self.assertEqual(self.engine.state(run["id"])["run"]["status"], "pausing")
            resume = await self.client.post(f"/api/runs/{run['id']}/control", json={"action": "resume"}, headers=self.headers)
            self.assertEqual(resume.status_code, 400)
            another = await self.client.post("/api/runs", json={"task": "second task"}, headers=self.headers)
            self.assertEqual(another.status_code, 400)
            configuration = await self.client.post("/api/settings", json={"thinking": False}, headers=self.headers)
            self.assertEqual(configuration.status_code, 400)
        finally:
            allow_cleanup.set()
            response = await asyncio.wait_for(pause_request, 5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paused")
        self.assertNotIn(run["id"], self.engine.boxes)

    async def test_external_a2a_input_invalidates_votes_and_wait_revision(self):
        run = await self.new_run()
        await self.pause(run["id"])
        state = self.engine.state(run["id"])
        previous_revision = state["revision"]
        previous_user_revision = state["user_revision"]
        for agent in state["agents"]:
            agent["vote"] = previous_revision
            agent["wait_revision"] = previous_user_revision
        response = await self.client.post("/a2a", headers={**self.headers, "A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": "steering-test", "method": "SendMessage", "params": {
                "message": {"messageId": "external-message", "role": "ROLE_USER", "taskId": run["id"],
                            "parts": [{"text": "Continue with this additional requirement"}]},
                "configuration": {"returnImmediately": True},
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("result", response.json(), response.text)
        self.assertEqual(state["revision"], previous_revision + 1)
        self.assertEqual(state["user_revision"], previous_user_revision + 1)
        self.assertTrue(all(agent["vote"] != state["revision"] for agent in state["agents"]))
        self.assertTrue(all(agent["wait_revision"] != state["user_revision"] for agent in state["agents"]))


    async def test_extending_run_limits_preserves_usage_and_changes_output_cap(self):
        run = await self.new_run()
        await self.pause(run["id"])
        state = self.engine.state(run["id"])
        state["usage"]["total_tokens"] = 1234
        state["run"]["active_seconds"] = 37
        response = await self.client.post(f"/api/runs/{run['id']}/limits", headers=self.headers, json={
            "max_rounds": 900, "max_tokens": 3000000, "max_minutes": 2000, "max_output_tokens": 16384,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(state["config"]["max_output_tokens"], 16384)
        self.assertEqual(state["usage"]["total_tokens"], 1234)
        self.assertEqual(state["run"]["active_seconds"], 37)
        self.assertEqual(self.engine.store.get(run["id"])["run"]["max_output_tokens"], 16384)
        await self.engine.control(run["id"], "stop")
        for action in ("resume", "pause"):
            response = await self.client.post(f"/api/runs/{run['id']}/control", headers=self.headers, json={"action": action})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(state["run"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
