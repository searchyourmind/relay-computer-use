"""Real-browser evidence checks: useful structure without application secrets."""
import json
import socket
import threading
import time

import pytest
import uvicorn

from relaycu.demo import app
from relaycu.models import Step, Target
from relaycu.surface import Policy, Surface


@pytest.fixture(scope="module")
def snapshot_origin():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    yield origin
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


@pytest.fixture
async def snapshot_surface(snapshot_origin):
    surface = Surface(Policy(origin=snapshot_origin))
    await surface.start(snapshot_origin)
    yield surface
    await surface.close()


async def test_snapshot_explains_disabled_and_hidden_controls_without_values(snapshot_surface):
    surface = snapshot_surface
    await surface.act(Step(action="fill", target=Target(kind="label", name="Member ID", frame="Member workspace"), input_ref="member_id"), {"member_id": "PRIVATE_83ad991"})
    await surface.page.frames[1].evaluate("""() => {
        document.querySelector('button').disabled = true;
        const heading = document.createElement('h2');
        heading.textContent = 'Accounts'; heading.style.display = 'none';
        document.body.append(heading);
        const hidden = document.createElement('button');
        hidden.textContent = 'Retry'; hidden.style.display = 'none';
        document.body.append(hidden);
        const secret = document.createElement('h2');
        secret.textContent = 'PRIVATE HISTORY: 999,999'; document.body.append(secret);
        const unknown = document.createElement('button');
        unknown.textContent = 'Delete PRIVATE_83ad991'; document.body.append(unknown);
    }""")

    snapshot = await surface.safe_snapshot()
    frames = {frame["scope"]: frame for frame in snapshot["structure"]["frames"]}
    assert set(frames) == {"main", "Member workspace"}
    workspace = frames["Member workspace"]
    controls = {entry["target"]["name"]: entry for entry in workspace["controls"]}
    assert controls["Find member"]["elements"] == [{"visible": True, "enabled": False}]
    assert controls["Retry"]["elements"] == [{"visible": False, "enabled": True}]
    headings = {entry["target"]["name"]: entry for entry in workspace["headings"]}
    assert headings["Accounts"]["elements"] == [{"visible": False}]
    assert controls["Member ID"]["match_count"] == 1
    serialized = json.dumps(snapshot)
    for secret in ["PRIVATE_83ad991", "PRIVATE HISTORY", "999,999", "Delete", "value", "innerHTML"]:
        assert secret not in serialized


async def test_snapshot_bounds_repeated_heading_evidence(snapshot_surface):
    await snapshot_surface.page.frames[1].evaluate("""() => {
        for (let i = 0; i < 30; i++) {
            const heading = document.createElement('h2');
            heading.textContent = 'Savings account';
            document.body.append(heading);
        }
    }""")
    snapshot = await snapshot_surface.safe_snapshot()
    workspace = next(frame for frame in snapshot["structure"]["frames"] if frame["scope"] == "Member workspace")
    entry = next(item for item in workspace["headings"] if item["target"]["name"] == "Savings account")
    assert entry["match_count"] == 30
    assert len(entry["elements"]) == 16
    assert entry["sample_truncated"] is True


async def test_policy_blocked_snapshot_does_not_inspect_application(snapshot_surface):
    snapshot_surface._blocked = True
    snapshot = await snapshot_surface.safe_snapshot()
    assert snapshot["observation"]["condition"] == "policy_blocked"
    assert snapshot["structure"] == {"visible_frames": 0, "visible_controls": 0, "frames": []}
