import asyncio
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarm.a2a import (
    A2AError, MAX_BODY_BYTES, MAX_TEXT_LENGTH, dispatch_a2a, install_a2a,
    parse_text, peer_message,
)


class FakeBackend:
    public_url = "http://127.0.0.1:8767"

    def __init__(self):
        self.sent = []
        self.task = {
            "id": "run-1", "contextId": "run-1",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": "2026-09-22T12:00:00Z"},
            "history": [
                {"messageId": str(i), "contextId": "run-1", "role": "ROLE_USER", "parts": [{"text": str(i)}]}
                for i in range(3)
            ],
            "artifacts": [{"artifactId": "out", "parts": [{"text": "result"}]}],
        }

    async def a2a_send(self, agent_id, params):
        self.sent.append((agent_id, deepcopy(params)))
        return {"task": deepcopy(self.task)}

    async def a2a_get(self, params):
        if params["id"] != self.task["id"]:
            raise KeyError(params["id"])
        return deepcopy(self.task)

    async def a2a_cancel(self, params):
        await self.a2a_get(params)
        if self.task["status"]["state"] == "TASK_STATE_CANCELED":
            raise A2AError(-32002, "Task is already canceled")
        self.task["status"]["state"] = "TASK_STATE_CANCELED"
        return deepcopy(self.task)

    async def a2a_list(self, params):
        return {"tasks": [deepcopy(self.task)], "nextPageToken": "", "pageSize": params.get("pageSize", 50), "totalSize": 1}


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(backend):
    app = FastAPI()
    install_a2a(app, backend)
    with TestClient(app, headers={"A2A-Version": "1.0"}) as result:
        yield result


def rpc(client, method, params=None, path="/a2a"):
    return client.post(path, json={"jsonrpc": "2.0", "id": "req-1", "method": method, "params": params or {}})


def test_cards_advertise_real_1_0_subset_and_configured_origin(client):
    response = client.get("/.well-known/agent-card.json", headers={"host": "evil.example"})
    card = response.json()
    assert card["supportedInterfaces"] == [{
        "url": "http://127.0.0.1:8767/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0",
    }]
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False, "extendedAgentCard": False}
    assert card["securitySchemes"]["swarmToken"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert client.get("/.well-known/agent-card.json", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    for n in range(1, 11):
        card = client.get(f"/a2a/agents/peer-{n:02d}/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["url"].endswith(f"/a2a/agents/peer-{n:02d}")
    assert client.get("/a2a/agents/boss/.well-known/agent-card.json").status_code == 404


def test_peer_message_and_send_round_trip(client, backend):
    envelope = peer_message("peer-01", "peer-02", "run-1", "Please review the experiment.")
    assert parse_text(envelope["params"]) == "Please review the experiment."
    assert envelope["params"]["message"]["role"] == "ROLE_USER"
    response = client.post("/a2a/agents/peer-02", json=envelope)
    result = response.json()
    assert result["id"] == envelope["id"]
    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert response.headers["A2A-Version"] == "1.0"
    assert backend.sent[0][0] == "peer-02"
    assert backend.sent[0][1]["message"]["metadata"]["sender"] == "peer-01"


def test_get_cancel_list_and_missing_task(client, backend):
    got = rpc(client, "GetTask", {"id": "run-1", "historyLength": 1}).json()["result"]
    assert len(got["history"]) == 1 and got["history"][0]["messageId"] == "2"
    assert "history" not in rpc(client, "GetTask", {"id": "run-1", "historyLength": 0}).json()["result"]
    listing = rpc(client, "ListTasks").json()["result"]
    assert listing["nextPageToken"] == "" and listing["totalSize"] == 1
    assert "artifacts" not in listing["tasks"][0]
    assert rpc(client, "ListTasks", {"includeArtifacts": True}).json()["result"]["tasks"][0]["artifacts"]
    assert backend.task["artifacts"]  # Filtering must not mutate durable data.
    missing = rpc(client, "GetTask", {"id": "missing"}).json()["error"]
    assert missing["code"] == -32001
    assert missing["data"][0]["reason"] == "TASK_NOT_FOUND"
    assert rpc(client, "CancelTask", {"id": "run-1"}).json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert rpc(client, "CancelTask", {"id": "run-1"}).json()["error"]["code"] == -32002


@pytest.mark.parametrize("body,code", [
    ("{", -32700), ("NaN", -32700), ("[]", -32600), ("null", -32600),
    ('{"jsonrpc":"1.0","id":1,"method":"GetTask"}', -32600),
    ('{"jsonrpc":"2.0","id":true,"method":"GetTask"}', -32600),
    ('{"jsonrpc":"2.0","id":[],"method":"GetTask"}', -32600),
    ('{"jsonrpc":"2.0","id":1,"method":"GetTask","params":[]}', -32602),
])
def test_invalid_json_rpc_never_crashes(client, body, code):
    response = client.post("/a2a", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("message", [
    None, [], {},
    {"messageId": "m", "role": "user", "parts": [{"text": "x"}]},
    {"messageId": "m", "role": "ROLE_USER", "parts": None},
    {"messageId": "m", "role": "ROLE_USER", "parts": []},
    {"messageId": "m", "role": "ROLE_USER", "parts": [None]},
    {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": 12}]},
    {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": " "}]},
    {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x", "data": {}}]},
    {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x" * (MAX_TEXT_LENGTH + 1)}]},
])
def test_malformed_messages_rejected_before_backend(client, backend, message):
    response = rpc(client, "SendMessage", {"message": message}).json()
    assert response["error"]["code"] == -32602
    assert backend.sent == []


@pytest.mark.parametrize("method,params,code", [
    ("SendStreamingMessage", {}, -32004), ("SubscribeToTask", {}, -32004),
    ("GetExtendedAgentCard", {}, -32004), ("CreateTaskPushNotificationConfig", {}, -32003),
    ("message/send", {}, -32601), ("Unknown", {}, -32601),
    ("GetTask", {"id": 2}, -32602), ("GetTask", {"id": "run-1", "historyLength": -1}, -32602),
    ("ListTasks", {"pageSize": 101}, -32602), ("ListTasks", {"pageSize": True}, -32602),
    ("ListTasks", {"status": []}, -32602), ("ListTasks", {"includeArtifacts": "yes"}, -32602),
    ("ListTasks", {"statusTimestampAfter": "2026-09-22"}, -32602),
    ("ListTasks", {"tenant": "other-tenant"}, -32004),
])
def test_method_and_parameter_errors(client, method, params, code):
    assert rpc(client, method, params).json()["error"]["code"] == code


def test_version_and_content_negotiation(client, backend):
    envelope = peer_message("peer-01", "peer-02", "run-1", "test")
    for version in ("", "0.3", "9.0"):
        assert client.post("/a2a", json=envelope, headers={"A2A-Version": version}).json()["error"]["code"] == -32009
    params = envelope["params"]
    params["message"]["parts"] = [{"url": "https://example.com/file.txt"}]
    assert rpc(client, "SendMessage", params).json()["error"]["code"] == -32005
    params["message"]["parts"] = [{"text": "test"}]
    params["configuration"]["taskPushNotificationConfig"] = {"url": "https://example.com"}
    assert rpc(client, "SendMessage", params).json()["error"]["code"] == -32003
    assert backend.sent == []


def test_size_limit_and_notifications(client, backend):
    assert client.post("/a2a", content=b" " * (MAX_BODY_BYTES + 1)).status_code == 413
    envelope = peer_message("peer-01", "peer-02", "run-1", "test")
    envelope.pop("id")
    response = client.post("/a2a", json=envelope)
    assert response.status_code == 204 and response.content == b""
    assert len(backend.sent) == 1


@pytest.mark.asyncio
async def test_default_send_waits_until_interrupted(backend):
    envelope = peer_message("peer-01", "peer-02", "run-1", "test")
    envelope["params"].pop("configuration")
    pending = asyncio.create_task(dispatch_a2a(backend, envelope, "peer-02"))
    await asyncio.sleep(0)
    assert not pending.done()
    backend.task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
    result = await asyncio.wait_for(pending, timeout=2)
    assert result["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_internal_exception_is_not_disclosed(backend):
    async def broken(params):
        raise RuntimeError("secret-api-key and private file path")
    backend.a2a_get = broken
    result = await dispatch_a2a(backend, {"jsonrpc": "2.0", "id": 7, "method": "GetTask", "params": {"id": "run-1"}})
    assert result == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "Internal error"}}
