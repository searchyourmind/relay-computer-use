"""Trusted edge proxy authentication and isolation; no browser or model required."""
import asyncio
from collections import deque
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from relaycu import server
from relaycu.models import Capability

HOST = "relay.example.test"
ORIGIN = f"https://{HOST}"
SECRET = b"test-only-secret-for-the-relay-edge-bridge"
VISITOR = hashlib.sha256(b"independent-browser-capability-one").hexdigest()
OTHER_VISITOR = hashlib.sha256(b"independent-browser-capability-two").hexdigest()
BODY = {"mode": "replay", "inputs": {"member_id": "1001"}}
CAPABILITY = Capability.model_validate_json(
    (Path(__file__).parents[1] / "evidence" / "capability.json").read_text()
)


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def signed(method, path, body=b"", *, visitor=VISITOR, timestamp=None, nonce=None, secret=SECRET):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_hex(16)
    message = "\n".join((stamp, nonce, visitor, method, path, hashlib.sha256(body).hexdigest()))
    return {"x-relay-visitor": visitor, "x-relay-timestamp": stamp, "x-relay-nonce": nonce,
            "x-relay-signature": hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()}


def bridge(client, method, path, value=None, *, visitor=VISITOR, extra_headers=None):
    body = b"" if value is None else encoded(value)
    headers = signed(method, path, body, visitor=visitor)
    if value is not None:
        headers["content-type"] = "application/json"
    headers.update(extra_headers or {})
    return client.request(method, path, content=body, headers=headers)


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    for name, value in {
        "HOSTED": True, "PUBLIC_HOST": HOST, "DISCOVERY_ENABLED": True,
        "BRIDGE_SECRET": SECRET, "BRIDGE_NONCES": {}, "MAX_HOSTED_CONCURRENT": 3,
        "ROOT": tmp_path, "LATEST": tmp_path / "runtime" / "latest-capability.json",
        "RUNS": {}, "TASKS": {}, "RUN_OWNERS": {}, "START_TIMES": {},
        "GLOBAL_START_TIMES": deque(),
    }.items():
        monkeypatch.setattr(server, name, value)
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
    with TestClient(server.app, base_url=ORIGIN) as client:
        yield client


def start(client, *, visitor=VISITOR, **changes):
    response = bridge(client, "POST", "/api/runs", BODY | changes, visitor=visitor)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_signed_edge_request_gets_scoped_identity_without_cookie_or_cors(hosted):
    response = bridge(hosted, "GET", "/api/config", extra_headers={"origin": "https://relay.lovable.app"})
    assert response.status_code == 200
    assert response.json()["hosted"] is True
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    assert SECRET.decode() not in response.text and VISITOR not in response.text
    run_id = start(hosted)
    assert server.RUN_OWNERS[run_id] == f"bridge:{VISITOR}"
    assert server.RUNS[run_id].inputs == BODY["inputs"]  # Auth preserved the raw JSON body.
    assert not hosted.cookies
    state = bridge(hosted, "GET", f"/api/runs/{run_id}")
    assert state.status_code == 200
    assert VISITOR not in state.text


@pytest.mark.parametrize("secret", [b"", b"too-short", b"x" * 31])
def test_bridge_disabled_without_strong_server_secret(hosted, monkeypatch, secret):
    monkeypatch.setattr(server, "BRIDGE_SECRET", secret)
    response = bridge(hosted, "GET", "/api/config")
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    # The public first-party experience remains usable when bridge is disabled.
    assert hosted.get("/api/config").status_code == 200


def test_bridge_is_not_enabled_in_local_mode(hosted, monkeypatch):
    monkeypatch.setattr(server, "HOSTED", False)
    response = bridge(hosted, "GET", "/api/config", extra_headers={"host": "testserver"})
    assert response.status_code == 403
    assert hosted.get("/api/config", headers={"host": "testserver"}).status_code == 200


@pytest.mark.parametrize("changed_header,value", [
    ("x-relay-visitor", "a" * 64),
    ("x-relay-timestamp", "1700000000"),
    ("x-relay-nonce", "b" * 32),
    ("x-relay-signature", "0" * 64),
])
def test_signature_tampering_never_falls_back_to_existing_cookie(hosted, changed_header, value):
    hosted.get("/api/config")
    assert hosted.cookies.get(server.COOKIE_NAME)
    headers = signed("GET", "/api/workspace")
    headers[changed_header] = value
    response = hosted.get("/api/workspace", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid bridge request"}
    assert "set-cookie" not in response.headers
    assert not server.BRIDGE_NONCES


@pytest.mark.parametrize("case", ["method", "path", "body", "visitor", "secret"])
def test_hmac_binds_method_path_body_and_identity(hosted, case):
    method, path, body = "POST", "/api/runs", encoded(BODY)
    kwargs = {}
    if case == "secret":
        kwargs["secret"] = b"another-server-key-with-enough-bytes"
    headers = signed("GET" if case == "method" else method, path, body, **kwargs)
    if case == "path":
        path = f"/api/runs/{'a' * 32}/abort"
    elif case == "body":
        body = encoded(BODY | {"inputs": {"member_id": "1002"}})
    elif case == "visitor":
        headers["x-relay-visitor"] = OTHER_VISITOR
    response = hosted.request(method, path, headers=headers, content=body)
    assert response.status_code == 403
    assert not server.RUNS and not server.BRIDGE_NONCES


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown", "uppercase_digest", "raw_token", "bad_nonce", "bad_timestamp"])
def test_malformed_bridge_headers_fail_closed(hosted, case):
    headers = signed("GET", "/api/config")
    if case == "missing":
        headers.pop("x-relay-signature")
    elif case == "duplicate":
        headers = list(headers.items()) + [("x-relay-visitor", VISITOR)]
    elif case == "unknown":
        headers["x-relay-extra"] = "unsupported"
    elif case == "uppercase_digest":
        headers["x-relay-signature"] = headers["x-relay-signature"].upper()
    elif case == "raw_token":
        headers["x-relay-visitor"] = secrets.token_urlsafe(32)
    elif case == "bad_nonce":
        headers["x-relay-nonce"] = "z" * 32
    else:
        headers["x-relay-timestamp"] = "1e12"
    response = hosted.get("/api/config", headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("offset", [-31, 31])
def test_expired_and_future_signatures_are_rejected(hosted, monkeypatch, offset):
    now = 1790000000
    monkeypatch.setattr(server.time, "time", lambda: now)
    headers = signed("GET", "/api/config", timestamp=now + offset)
    assert hosted.get("/api/config", headers=headers).status_code == 403
    valid = signed("GET", "/api/config", timestamp=now)
    assert hosted.get("/api/config", headers=valid).status_code == 200


def test_nonce_is_single_use_even_for_a_different_validly_signed_request(hosted):
    headers = signed("GET", "/api/config")
    assert hosted.get("/api/config", headers=headers).status_code == 200
    assert hosted.get("/api/config", headers=headers).status_code == 403
    reused = signed("GET", "/api/workspace", nonce=headers["x-relay-nonce"], visitor=OTHER_VISITOR)
    assert hosted.get("/api/workspace", headers=reused).status_code == 403
    assert bridge(hosted, "GET", "/api/workspace").status_code == 200


def test_invalid_signature_cannot_consume_a_legitimate_nonce(hosted):
    headers = signed("GET", "/api/config")
    invalid = headers | {"x-relay-signature": "f" * 64}
    assert hosted.get("/api/config", headers=invalid).status_code == 403
    assert hosted.get("/api/config", headers=headers).status_code == 200


@pytest.mark.parametrize("already_used", [False, True])
async def test_slow_body_cannot_revive_expired_signature_or_nonce(monkeypatch, already_used):
    clock = {"wall": 1790000000, "monotonic": 100}
    monkeypatch.setattr(server, "HOSTED", True)
    monkeypatch.setattr(server, "BRIDGE_SECRET", SECRET)
    monkeypatch.setattr(server, "BRIDGE_NONCES", {})
    monkeypatch.setattr(server, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]
    ))
    headers = signed("GET", "/api/config", timestamp=clock["wall"])

    def request(delay):
        async def receive():
            clock["wall"] += delay
            clock["monotonic"] += delay
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request({"type": "http", "method": "GET", "path": "/api/config",
                        "raw_path": b"/api/config", "query_string": b"", "scheme": "https",
                        "server": (HOST, 443), "headers": [(k.encode(), v.encode()) for k, v in headers.items()]},
                       receive=receive)

    if already_used:
        assert await server.bridge_visitor(request(0)) == f"bridge:{VISITOR}"
    # The timestamp is valid before receive(), but expires while reading. With
    # a previous request, its cached nonce also expires during that same read.
    assert await server.bridge_visitor(request(62)) is None


def test_nonce_cache_is_bounded_and_only_expired_entries_are_reclaimed(hosted, monkeypatch):
    monkeypatch.setattr(server, "BRIDGE_NONCE_LIMIT", 1)
    assert bridge(hosted, "GET", "/api/config").status_code == 200
    assert bridge(hosted, "GET", "/api/config").status_code == 403
    assert len(server.BRIDGE_NONCES) == 1
    old_nonce = next(iter(server.BRIDGE_NONCES))
    server.BRIDGE_NONCES[old_nonce] = time.monotonic() - 1
    assert bridge(hosted, "GET", "/api/config").status_code == 200
    assert old_nonce not in server.BRIDGE_NONCES
    assert len(server.BRIDGE_NONCES) == 1


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/health"), ("GET", "/configure"), ("GET", "/assets/app.js"),
    ("GET", "/api/runs"), ("POST", "/api/config"), ("OPTIONS", "/api/runs"),
    ("GET", "/api/config?ignored=1"), ("GET", "/%61pi/config"),
    ("GET", "/api/runs/not-a-run-id"), ("POST", f"/api/runs/{'a' * 32}/unknown"),
])
def test_bridge_has_exact_routes_and_rejects_query_or_encoded_path(hosted, method, path):
    response = bridge(hosted, method, path)
    assert response.status_code == 403
    assert not server.BRIDGE_NONCES
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("host", ["relay.example.test.attacker.test", "127.0.0.1:4310", "healthcheck.railway.app"])
def test_valid_signature_cannot_bypass_host_boundary(hosted, host):
    response = bridge(hosted, "GET", "/api/config", extra_headers={"host": host})
    assert response.status_code == 403
    assert not server.BRIDGE_NONCES


@pytest.mark.parametrize("chunked", [False, True])
def test_bridge_body_limit_is_enforced_before_downstream_parsing(hosted, chunked):
    raw = b"x" * (server.BRIDGE_BODY_BYTES + 1)
    headers = signed("POST", "/api/runs", raw)
    content = iter([raw[:100], raw[100:]]) if chunked else raw
    response = hosted.post("/api/runs", content=content, headers=headers)
    assert response.status_code == 403
    assert not server.RUNS and not server.BRIDGE_NONCES


def test_chunked_raw_body_is_authenticated_then_replayed_for_fastapi_parsing(hosted):
    # Whitespace and transport chunk boundaries are deliberately different from
    # the helper's compact JSON: the signature must cover the exact raw bytes.
    raw = json.dumps(BODY, indent=2).encode()
    headers = signed("POST", "/api/runs", raw) | {"content-type": "application/json"}
    response = hosted.post("/api/runs", content=iter([raw[:7], raw[7:19], raw[19:]]), headers=headers)
    assert response.status_code == 202, response.text
    assert server.RUNS[response.json()["id"]].inputs == BODY["inputs"]
    assert "set-cookie" not in response.headers


def test_bridge_and_cookie_visitors_cannot_read_or_control_each_others_runs(hosted):
    own = start(hosted)
    other = start(hosted, visitor=OTHER_VISITOR, inputs={"member_id": "1002"})
    first_party = hosted.post("/api/runs", json=BODY, headers={"origin": ORIGIN})
    assert first_party.status_code == 202
    cookie_run = first_party.json()["id"]
    for visitor, visible, hidden in [(VISITOR, own, other), (OTHER_VISITOR, other, own)]:
        workspace = bridge(hosted, "GET", "/api/workspace", visitor=visitor).json()
        assert [run["id"] for run in workspace["runs"]] == [visible]
        assert workspace["active_run_id"] == visible
        for inaccessible in [hidden, cookie_run]:
            assert bridge(hosted, "GET", f"/api/runs/{inaccessible}", visitor=visitor).status_code == 404
            for action in ["claim", "operator", "resume", "abort"]:
                response = bridge(hosted, "POST", f"/api/runs/{inaccessible}/{action}",
                                  {"action": "retry"} if action == "operator" else None, visitor=visitor)
                assert response.status_code == 404
                assert response.json() == {"detail": "Run not found"}
    assert hosted.get(f"/api/runs/{own}").status_code == 404
    assert [run["id"] for run in hosted.get("/api/workspace").json()["runs"]] == [cookie_run]
    assert bridge(hosted, "POST", f"/api/runs/{own}/abort").status_code == 200
    assert server.RUNS[own].aborted is True
    assert server.RUNS[other].aborted is False
    assert server.RUNS[cookie_run].aborted is False


def test_bridge_discovery_artifact_remains_visitor_scoped(hosted):
    run_id = start(hosted, mode="discover")
    run = server.RUNS[run_id]
    run.capability = CAPABILITY.model_copy(update={"revision": 42})
    run.finish("success", "verified")
    own = bridge(hosted, "GET", "/api/capability").json()
    assert own["source"] == "discovery" and own["capability"]["revision"] == 42
    assert bridge(hosted, "GET", "/api/capability", visitor=OTHER_VISITOR).json()["source"] == "example"
    assert hosted.get("/api/capability").json()["source"] == "example"
    replay_id = start(hosted)
    assert server.RUNS[replay_id].capability.revision == 42


def test_bridge_keeps_fixed_inputs_rate_limits_and_browser_capacity(hosted, monkeypatch):
    response = bridge(hosted, "POST", "/api/runs", BODY | {"inputs": {"member_id": "private-account"}})
    assert response.status_code == 422 and not server.RUNS
    monkeypatch.setattr(server, "VISITOR_START_LIMIT", 1)
    monkeypatch.setattr(server, "MAX_HOSTED_CONCURRENT", 1)
    run_id = start(hosted)
    assert bridge(hosted, "POST", "/api/runs", BODY).status_code == 409
    assert bridge(hosted, "POST", "/api/runs", BODY, visitor=OTHER_VISITOR).status_code == 429
    assert bridge(hosted, "POST", f"/api/runs/{run_id}/abort").status_code == 200
    assert bridge(hosted, "POST", "/api/runs", BODY).status_code == 429
    start(hosted, visitor=OTHER_VISITOR)
