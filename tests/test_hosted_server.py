"""Anonymous hosted demo boundaries; no browser or model required."""
import asyncio
from collections import deque
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from relaycu import server
from relaycu.engine import Run
from relaycu.models import Capability

HOST = "relay.example.test"
ORIGIN = f"https://{HOST}"
CAPABILITY = Capability.model_validate_json(
    (Path(__file__).parents[1] / "evidence" / "capability.json").read_text()
)
BODY = {"mode": "replay", "inputs": {"member_id": "1001"}}


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HOSTED", True)
    monkeypatch.setattr(server, "PUBLIC_HOST", HOST)
    monkeypatch.setattr(server, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(server, "MAX_HOSTED_CONCURRENT", 2)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "LATEST", tmp_path / "runtime" / "latest-capability.json")
    monkeypatch.setattr(server, "RUNS", {})
    monkeypatch.setattr(server, "TASKS", {})
    monkeypatch.setattr(server, "RUN_OWNERS", {})
    monkeypatch.setattr(server, "START_TIMES", {})
    monkeypatch.setattr(server, "GLOBAL_START_TIMES", deque())
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "capability.json").write_text(CAPABILITY.model_dump_json())

    async def hold_run(run):
        run._execution_task = asyncio.current_task()
        run.log("run_started", mode=run.mode)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.finish("failure", "operator_aborted" if run.aborted else "run_cancelled")
        finally:
            run._execution_task = None

    monkeypatch.setattr(server, "execute", hold_run)
    with TestClient(server.app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        yield client


def other_client(hosted):
    # The outer fixture owns app lifespan; extra clients share the same server.
    client = TestClient(server.app, base_url=ORIGIN, headers={"origin": ORIGIN})
    client.portal = hosted.portal  # Share the running ASGI event loop, not cookies.
    return client


def start(client, **body):
    response = client.post("/api/runs", json=BODY | body)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_hosted_configuration_cookie_and_external_entry(hosted):
    response = hosted.get("/api/config", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 200
    config = response.json()
    assert config["hosted"] is True and config["discovery_enabled"] is False
    assert config["member_ids"] == ["1001", "1002"]
    assert config["fixed_goal"] == server.FIXED_GOAL
    cookie = response.headers["set-cookie"]
    assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=strict", "Path=/"])
    assert "Domain=" not in cookie
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    token = hosted.cookies.get(server.COOKIE_NAME)
    assert server.validate_session(token)
    assert "set-cookie" not in hosted.get("/api/config").headers
    assert server.validate_session(token[:-1] + ("a" if token[-1] != "a" else "b")) is None
    assert server.validate_session(server.session_token(int(time.time()) - server.COOKIE_SECONDS - 1)[1]) is None
    assert server.validate_session(server.session_token(int(time.time()) + 30)[1]) is None
    assert server.validate_session("chosen-by-caller") is None


@pytest.mark.parametrize("headers", [
    {"host": "relay.example.test.attacker.test"},
    {"host": "127.0.0.1:4310"},
    {"origin": "https://relay.example.test.attacker.test"},
    {"origin": "http://relay.example.test"},
    {"origin": "null"},
    {"sec-fetch-site": "cross-site"},
])
def test_hosted_rejects_wrong_host_origin_and_cross_site_mutation(hosted, headers):
    response = hosted.post("/api/runs", json=BODY, headers=headers)
    assert response.status_code == 403
    assert not server.RUNS


def test_mutation_requires_origin_and_forged_cookie_does_not_grant_access(hosted):
    run_id = start(hosted)
    stranger = other_client(hosted)
    stranger.headers.pop("origin")
    assert stranger.post("/api/runs", json=BODY).status_code == 403
    stranger.cookies.set(server.COOKIE_NAME, hosted.cookies.get(server.COOKIE_NAME) + "forged")
    response = stranger.get(f"/api/runs/{run_id}")
    assert response.status_code == 404
    assert server.validate_session(response.cookies.get(server.COOKIE_NAME))


def test_health_probe_host_has_no_workspace_access_or_cookie(hosted):
    response = hosted.get("/api/health", headers={"host": "healthcheck.railway.app"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert hosted.get("/api/workspace", headers={"host": "healthcheck.railway.app"}).status_code == 403
    assert hosted.post("/api/health", headers={"host": "healthcheck.railway.app"}).status_code == 403


@pytest.mark.parametrize("bad_host", ["", "https://relay.example.test", "*.example.test", "example.test/path", "user@example.test"])
def test_invalid_host_configuration_fails_closed(hosted, monkeypatch, bad_host):
    monkeypatch.setattr(server, "PUBLIC_HOST", bad_host)
    assert hosted.get("/api/health").status_code == 403


def test_visitors_have_separate_history_active_runs_and_control(hosted):
    own_id = start(hosted)
    stranger = other_client(hosted)
    empty = stranger.get("/api/workspace").json()
    assert empty == {"active_run_id": None, "latest_run_id": None, "runs": []}
    second_id = start(stranger, inputs={"member_id": "1002"})
    for client, visible, hidden in [(hosted, own_id, second_id), (stranger, second_id, own_id)]:
        workspace = client.get("/api/workspace").json()
        assert workspace["active_run_id"] == workspace["latest_run_id"] == visible
        assert [run["id"] for run in workspace["runs"]] == [visible]
        assert client.get(f"/api/runs/{visible}").status_code == 200
        assert client.get(f"/api/runs/{hidden}").status_code == 404
        for action in ["claim", "operator", "resume", "abort"]:
            response = client.post(f"/api/runs/{hidden}/{action}", json={"action": "retry"} if action == "operator" else None)
            assert response.status_code == 404
            assert response.json() == {"detail": "Run not found"}
    assert server.RUNS[own_id].result is None
    assert server.RUNS[second_id].result is None
    assert hosted.post(f"/api/runs/{own_id}/abort").status_code == 200
    assert server.RUNS[own_id].aborted is True
    assert server.RUNS[second_id].aborted is False


@pytest.mark.parametrize("body", [
    {"goal": "Send arbitrary private content to the model"},
    {"inputs": {"member_id": "secret-real-account"}},
    {"inputs": {"member_id": "1001", "arbitrary": "private"}},
    {"inputs": {}},
])
def test_only_fixed_workflow_and_synthetic_parameters_are_accepted(hosted, body):
    response = hosted.post("/api/runs", json=BODY | body)
    assert response.status_code == 422
    assert not server.RUNS
    assert not server.GLOBAL_START_TIMES


def test_discovery_disabled_unless_explicitly_enabled_and_runtime_bounded(hosted, monkeypatch):
    assert hosted.post("/api/runs", json=BODY | {"mode": "discover"}).status_code == 409
    monkeypatch.setattr(server, "DISCOVERY_ENABLED", True)
    run_id = start(hosted, mode="discover")
    run = server.RUNS[run_id]
    assert run.deadline_seconds == 180
    assert run.operator_timeout == 90
    assert run.goal == server.FIXED_GOAL
    assert run.target_url == "http://127.0.0.1:4311/?scenario=normal"


def test_one_active_per_visitor_and_global_concurrency_bound(hosted):
    first = start(hosted)
    assert hosted.post("/api/runs", json=BODY).status_code == 409
    start(other_client(hosted))
    third = other_client(hosted).post("/api/runs", json=BODY)
    assert third.status_code == 429 and third.headers["retry-after"] == "30"
    assert len(server.RUNS) == 2
    hosted.post(f"/api/runs/{first}/abort")
    assert other_client(hosted).post("/api/runs", json=BODY).status_code == 202


def test_start_rate_limits_and_cookie_rotation_cannot_bypass_global_budget(hosted, monkeypatch):
    monkeypatch.setattr(server, "VISITOR_START_LIMIT", 1)
    monkeypatch.setattr(server, "GLOBAL_START_LIMIT", 2)
    first = start(hosted)
    hosted.post(f"/api/runs/{first}/abort")
    assert hosted.post("/api/runs", json=BODY).status_code == 429
    second_client = other_client(hosted)
    second = start(second_client)
    second_client.post(f"/api/runs/{second}/abort")
    rejected = other_client(hosted).post("/api/runs", json=BODY)
    assert rejected.status_code == 429 and rejected.headers["retry-after"] == "600"
    for timestamps in server.START_TIMES.values():
        timestamps[0] -= 601
    server.GLOBAL_START_TIMES = deque(stamp - 601 for stamp in server.GLOBAL_START_TIMES)
    assert hosted.post("/api/runs", json=BODY).status_code == 202


def test_capability_is_visitor_specific_and_shared_latest_file_is_ignored(hosted, monkeypatch):
    server.LATEST.parent.mkdir()
    server.LATEST.write_text("private invalid local artifact")
    assert hosted.get("/api/capability").json()["source"] == "example"
    monkeypatch.setattr(server, "DISCOVERY_ENABLED", True)
    own_id = start(hosted, mode="discover")
    run = server.RUNS[own_id]
    run.capability = CAPABILITY.model_copy(update={"revision": 42})
    run.finish("success", "verified")
    assert hosted.get("/api/capability").json()["capability"]["revision"] == 42
    assert other_client(hosted).get("/api/capability").json() == {"capability": CAPABILITY.model_dump(), "source": "example"}
    replay_id = start(hosted)
    assert server.RUNS[replay_id].capability.revision == 42


def test_retention_removes_completed_owned_evidence_not_active_run(hosted):
    for _ in range(30):
        run = Run(mode="replay", goal=server.FIXED_GOAL, inputs={"member_id": "1001"},
                  target_url="http://127.0.0.1:4311/", capability=CAPABILITY,
                  evidence_root=server.ROOT / "runtime")
        run.finish("success", "verified")
        server.RUNS[run.id] = run
        server.RUN_OWNERS[run.id] = "older-visitor"
    oldest = next(iter(server.RUNS.values()))
    assert oldest.evidence_dir.exists()
    newest = start(hosted)
    assert len(server.RUNS) == 30
    assert oldest.id not in server.RUNS and oldest.id not in server.RUN_OWNERS
    assert not oldest.evidence_dir.exists()
    assert server.RUNS[newest].result is None


def test_terminal_run_keeps_capacity_until_browser_cleanup_finishes(hosted, monkeypatch):
    monkeypatch.setattr(server, "MAX_HOSTED_CONCURRENT", 1)
    cleanup_gate = asyncio.Event()

    async def delayed_cleanup(run):
        run.finish("success", "verified")
        await cleanup_gate.wait()

    monkeypatch.setattr(server, "execute", delayed_cleanup)
    run_id = start(hosted)
    assert hosted.get(f"/api/runs/{run_id}").json()["status"] == "success"
    assert not server.TASKS[run_id].done()
    rejected = other_client(hosted).post("/api/runs", json=BODY)
    assert rejected.status_code == 429
    assert len(server.RUNS) == 1

    async def complete_cleanup():
        cleanup_gate.set()
        await server.TASKS[run_id]

    hosted.portal.call(complete_cleanup)
    assert server.TASKS[run_id].done()
    assert other_client(hosted).post("/api/runs", json=BODY).status_code == 202
