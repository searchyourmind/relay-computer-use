"""Check the synthetic browser fixture's contract, faults, and session isolation."""

import pytest
from fastapi.testclient import TestClient

from relaycu.demo import app


def open_member(client, member="1001", scenario="normal"):
    response = client.get("/", params={"scenario": scenario})
    assert 'title="Member workspace"' in response.text
    assert 'type="application/json"' not in response.text
    client.get("/workspace")
    return client.post("/workspace/find", data={"member_id": member})


@pytest.mark.parametrize("member,balance", [("1001", "4,250.75"), ("1002", "9,180.20")])
def test_member_flow(member, balance):
    with TestClient(app) as client:
        assert "Open profile" in open_member(client, member).text
        assert "Member overview" in client.get("/workspace/profile").text
        assert "Open savings" in client.get("/workspace/accounts").text
        result = client.get("/workspace/savings")
        assert f'<output aria-label="Current balance">{balance}</output>' in result.text


def test_sessions_do_not_share_members():
    with TestClient(app) as first, TestClient(app) as second:
        open_member(first, "1001")
        open_member(second, "1002")
        assert "4,250.75" in first.get("/workspace/savings").text
        assert "9,180.20" in second.get("/workspace/savings").text


@pytest.mark.parametrize("scenario,message", [
    ("permission", "Permission denied"),
    ("error", "Application error"),
    ("dialog", "Unexpected confirmation"),
])
def test_blocking_states(scenario, message):
    with TestClient(app) as client:
        open_member(client, scenario=scenario)
        response = client.get("/workspace/profile")
        assert message in response.text
        assert 'href="/workspace/accounts"' not in response.text


@pytest.mark.parametrize("scenario,message,button", [
    ("session", "Session expired", "Restore session"),
    ("transient", "Service temporarily unavailable", "Retry"),
])
def test_recovery_resumes_original_member(scenario, message, button):
    with TestClient(app) as client:
        open_member(client, "1002", scenario)
        for _ in range(2):
            response = client.get("/workspace/profile")
            assert message in response.text
            assert f'>{button}</button>' in response.text
        response = client.post("/workspace/resume")
        assert response.url.path == "/workspace/profile"
        assert "Member overview" in response.text
        assert "Morgan Sample" in response.text
        assert "9,180.20" in client.get("/workspace/savings").text


def test_not_found_and_ambiguous_states():
    with TestClient(app) as client:
        assert "Member not found" in open_member(client, scenario="not_found").text
        assert open_member(client, scenario="ambiguous").text.count('>Open profile</a>') == 2


def test_unknown_routes_and_missing_session():
    with TestClient(app) as client:
        assert "Session expired" in client.get("/workspace/savings").text
        assert client.get("/?scenario=unknown").status_code == 400
        assert client.get("/api/accounts").status_code == 404
        assert client.get("/docs").status_code == 404
