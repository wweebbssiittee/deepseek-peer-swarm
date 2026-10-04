"""Keep the browser fixture isolated even when a developer has real credentials."""
import json
from pathlib import Path

import httpx
import pytest

from scripts.browser_fixture import FixtureProvider, isolated_server, public_fixture


@pytest.mark.asyncio
async def test_fake_provider_can_only_coordinate_and_reports_zero_spend():
    provider = FixtureProvider()
    for index in range(1, 11):
        response, usage = await provider.complete(f"offline-fixture-{index:02}", {}, [], [])
        assert "[OFFLINE FIXTURE]" in response["content"]
        assert [call["function"]["name"] for call in response["tool_calls"]] == ["create_work", "yield_work"]
        assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    response, _ = await provider.complete("offline-fixture-01", {}, [], [])
    assert response["content"] is None
    assert [call["function"]["name"] for call in response["tool_calls"]] == ["yield_work"]


def test_screenshot_fixture_sanitizes_nested_and_serialized_paths(tmp_path):
    source = {"run": {"workspace": str(tmp_path / "workspaces" / "fixture")},
              "events": [{"payload": json.dumps({"workspace": str(tmp_path / "workspaces")})}]}
    cleaned = public_fixture(source, tmp_path)
    assert cleaned["run"]["workspace"].startswith("demo-workspace")
    assert str(tmp_path) not in json.dumps(cleaned)
    assert str(tmp_path) not in cleaned["events"][0]["payload"]
    assert source["run"]["workspace"].startswith(str(tmp_path)), "Sanitization must not mutate engine state"


def test_fixture_ignores_environment_credentials_and_removes_its_state(tmp_path, monkeypatch):
    private_home = tmp_path / "must-not-read-runtime"
    monkeypatch.setenv("SWARM_DATA_DIR", str(private_home))
    for index in range(1, 11):
        monkeypatch.setenv(f"DEEPSEEK_API_KEY_{index}", "test-sentinel-not-a-real-credential")
    with isolated_server() as (url, home, provider):
        assert home != private_home
        with httpx.Client(base_url=url, trust_env=False) as client:
            bootstrap = client.get("/api/bootstrap").json()
            headers = {"X-Swarm-Token": bootstrap["csrf_token"]}
            # Duplicated sentinel values would fail the ten-distinct-keys check
            # if the fixture accidentally retained the developer environment.
            response = client.post("/api/runs", headers=headers, json={
                "task": "[OFFLINE FIXTURE] Verify isolated startup",
                "permissions": {"read_files": False, "write_files": False,
                                "internet": False, "commands": "deny", "deploy": "deny"},
            })
            assert response.status_code == 200
            run = response.json()
            assert Path(run["workspace"]).is_relative_to(home)
            paused = client.post(f"/api/runs/{run['id']}/control", headers=headers, json={"action": "pause"})
            assert paused.status_code == 200
            assert paused.json()["status"] == "paused"
            snapshot = client.get(f"/api/runs/{run['id']}").json()
            assert snapshot["billing"]["spent_usd"] == "0.000000000"
    assert not home.exists()
    assert not private_home.exists()
