"""Console routing, reconnect and privacy boundaries without a browser or model."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaycu import server
from relaycu.engine import Run
from relaycu.models import Capability


CAPABILITY = Capability.model_validate_json(
    (Path(__file__).parents[1] / "evidence" / "capability.json").read_text()
)


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "LATEST", tmp_path / "runtime" / "latest-capability.json")
    monkeypatch.setattr(server, "RUNS", {})
    monkeypatch.setattr(server, "TASKS", {})
    static = tmp_path / "static"
    (static / "pages").mkdir(parents=True)
    (static / "shell.html").write_text(
        '<!doctype html><title>@@TITLE@@</title><body data-page="@@PAGE@@">'
        '<h1>@@HEADING@@</h1><p>@@SUBTITLE@@</p>@@CONTENT@@</body>'
    )
    for page in server.PAGES:
        (static / "pages" / f"{page}.html").write_text(f'<main id="{page}-content"></main>')
    (static / "app.css").write_text("body { color: white; }")
    (static / "app.js").write_text("console.log('Relay');")
    monkeypatch.setattr(server, "STATIC", static)
    assets = next(route.app for route in server.app.routes if route.name == "assets")
    monkeypatch.setattr(assets, "directory", static)
    monkeypatch.setattr(assets, "all_directories", [static])
    with TestClient(server.app) as client:
        yield client


def save_capability(path, capability=CAPABILITY):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(capability.model_dump_json())


def run_fixture(mode="replay"):
    return Run(mode=mode, goal="PRIVATE-GOAL", inputs={"member_id": "PRIVATE-MEMBER"},
               capability=CAPABILITY if mode == "replay" else None,
               target_url="http://127.0.0.1:4311/", evidence_root=server.ROOT / "runtime")


@pytest.mark.parametrize("page", ["configure", "execution", "activity", "workflows"])
def test_pages_support_direct_navigation_and_reload(console, page):
    for _ in range(2):
        response = console.get(f"/{page}?untrusted=<script>bad()</script>")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert f'data-page="{page}"' in response.text
        assert f'id="{page}-content"' in response.text
        assert response.text.count("-content") == 1
        assert "@@" not in response.text
        assert "bad()" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_root_redirect_static_assets_and_unknown_paths(console):
    response = console.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/configure"
    assert console.get("/assets/app.css").status_code == 200
    assert console.get("/assets/app.js").status_code == 200
    assert console.get("/not-a-page").status_code == 404
    assert console.get("/pages/../../server.py").status_code == 404
    assert console.get("/assets/%2e%2e/server.py").status_code == 404


@pytest.mark.parametrize("path,headers", [
    ("/configure", {"host": "attacker.example:4310"}),
    ("/api/workspace", {"host": "127.0.0.1:4310.attacker.example"}),
    ("/api/capability", {"origin": "https://attacker.example"}),
    ("/assets/app.js", {"origin": "null"}),
])
def test_loopback_host_and_origin_boundary(console, path, headers):
    assert console.get(path, headers=headers).status_code == 403


def test_allowed_origin_and_security_headers(console):
    response = console.get("/api/health", headers={"host": "localhost:4310", "origin": "http://localhost:4310"})
    assert response.json() == {"status": "ok", "service": "relay", "synthetic_demo_only": True}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_workspace_is_empty_before_first_run(console):
    assert console.get("/api/workspace").json() == {"active_run_id": None, "latest_run_id": None, "runs": []}


def test_workspace_exposes_redacted_history_and_real_counters(console):
    completed = run_fixture()
    completed.log("run_started", input_fields=["member_id"])
    completed.step, completed.recoveries, completed.handoffs = 5, 2, 1
    completed.finish("success", "verified", {"current_balance": "PRIVATE-BALANCE"})
    active = run_fixture("discover")
    active.log("run_started", input_fields=["member_id"])
    active.status, active.owner = "human_control", "human"
    server.RUNS.update({completed.id: completed, active.id: active})

    response = console.get("/api/workspace")
    state = response.json()
    assert state["active_run_id"] == state["latest_run_id"] == active.id
    assert [run["id"] for run in state["runs"]] == [active.id, completed.id]
    recent, previous = state["runs"]
    assert recent["finished_at"] is None
    assert recent["owner"] == "human"
    assert previous["started_at"] == completed.events[0]["at"]
    assert previous["finished_at"] == completed.events[-1]["at"]
    assert (previous["recoveries"], previous["handoffs"], previous["step"]) == (2, 1, 5)
    assert set(previous) == {"id", "mode", "status", "owner", "step", "model_calls", "started_at", "finished_at", "recoveries", "handoffs"}
    assert "PRIVATE-" not in response.text
    assert "input_fields" not in response.text


def test_capability_example_latest_precedence_and_missing(console):
    assert console.get("/api/capability").json() == {"capability": None, "source": None}
    save_capability(server.ROOT / "evidence" / "capability.json")
    assert console.get("/api/capability").json() == {"capability": CAPABILITY.model_dump(), "source": "example"}
    discovered = CAPABILITY.model_copy(update={"revision": 2})
    save_capability(server.LATEST, discovered)
    assert console.get("/api/capability").json() == {"capability": discovered.model_dump(), "source": "discovery"}


@pytest.mark.parametrize("content", [b'{"secret": "PRIVATE-BAD-CONTENT"}', b"not json PRIVATE-BAD-CONTENT", b"\xff\xfe"])
def test_malformed_latest_is_sanitized_and_never_silently_falls_back(console, content):
    save_capability(server.ROOT / "evidence" / "capability.json")
    server.LATEST.parent.mkdir(parents=True)
    server.LATEST.write_bytes(content)
    for response in [console.get("/api/capability"), console.post("/api/runs", json={"mode": "replay", "inputs": {"member_id": "1001"}})]:
        assert response.status_code == 503
        assert "PRIVATE-BAD-CONTENT" not in response.text
        assert str(server.ROOT) not in response.text
    assert server.RUNS == {}


def test_malformed_example_and_unreadable_latest_are_sanitized(console):
    example = server.ROOT / "evidence" / "capability.json"
    example.parent.mkdir()
    example.write_text("invalid artifact")
    assert console.get("/api/capability").status_code == 503
    server.LATEST.mkdir(parents=True)
    assert console.get("/api/capability").status_code == 503


def test_replay_requires_saved_capability(console):
    response = console.post("/api/runs", json={"mode": "replay", "inputs": {"member_id": "1001"}})
    assert response.status_code == 409
    assert server.RUNS == {}


def test_navigation_reconnects_same_active_run_without_starting_another(console, monkeypatch):
    async def hold_run(run):
        run.log("run_started", mode=run.mode)
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "execute", hold_run)
    save_capability(server.ROOT / "evidence" / "capability.json")
    body = {"mode": "replay", "inputs": {"member_id": "1002"}}
    response = console.post("/api/runs", json=body)
    assert response.status_code == 202
    run_id = response.json()["id"]
    session_id = console.get(f"/api/runs/{run_id}").json()["session_id"]
    for page in ["execution", "activity", "workflows", "execution"]:
        assert console.get(f"/{page}").status_code == 200
        assert console.get("/api/workspace").json()["active_run_id"] == run_id
        assert console.get(f"/api/runs/{run_id}").json()["session_id"] == session_id
    assert console.post("/api/runs", json=body).status_code == 409
    assert list(server.RUNS) == [run_id]
    assert server.RUNS[run_id].planner is None
    assert server.RUNS[run_id].inputs == {"member_id": "1002"}
    assert server.RUNS[run_id].capability == CAPABILITY


def test_existing_run_control_contract_is_preserved(console):
    run = run_fixture()
    server.RUNS[run.id] = run
    response = console.get(f"/api/runs/{run.id}")
    assert response.json() == run.public_state()
    assert console.post(f"/api/runs/{run.id}/claim").status_code == 409
    assert console.post(f"/api/runs/{run.id}/operator", json={"action": "restore_session"}).status_code == 409
    run.owner, run.status = "awaiting_operator", "awaiting_operator"
    response = console.post(f"/api/runs/{run.id}/claim")
    assert response.json()["owner"] == "human"
    assert response.json()["events"][-1]["event"] == "control_transferred"
    assert console.post(f"/api/runs/{run.id}/operator", json={"action": "arbitrary_script"}).status_code == 422
    assert console.post(f"/api/runs/{run.id}/abort").status_code == 200
    assert run.aborted is True


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/runs/missing", None),
    ("post", "/api/runs/missing/claim", None),
    ("post", "/api/runs/missing/operator", {"action": "retry"}),
    ("post", "/api/runs/missing/resume", None),
    ("post", "/api/runs/missing/abort", None),
])
def test_missing_runs_remain_not_found(console, method, path, body):
    response = console.request(method, path, json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}
