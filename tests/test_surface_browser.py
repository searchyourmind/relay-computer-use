"""Real Chromium checks against the HTML-only synthetic application."""
import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn

from relaycu.demo import app
from relaycu.models import Step, Target
from relaycu.surface import Policy, Surface, SurfaceError


@pytest.fixture(scope="module")
def demo_origin():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield origin
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture
async def surface(demo_origin):
    instance = Surface(Policy(origin=demo_origin))
    yield instance
    await instance.close()


def target(name, role="link"):
    return Target(kind="role", role=role, name=name, frame="Member workspace")


async def click(surface, name, role="link", operator=False):
    step = Step(action="click", target=target(name, role))
    await (surface.operator_act(step, {}) if operator else surface.act(step, {}))


async def select_member(surface, member="1001"):
    await surface.act(Step(action="fill", target=Target(kind="label", name="Member ID", frame="Member workspace"), input_ref="member_id"), {"member_id": member})
    await click(surface, "Find member", "button")


async def open_profile(surface, demo_origin, scenario="normal"):
    await surface.start(f"{demo_origin}/?scenario={scenario}")
    await select_member(surface)
    await click(surface, "Open profile")


async def test_real_iframe_flow_and_redacted_observation(surface, demo_origin):
    await surface.start(demo_origin)
    first = await surface.observe()
    second = await surface.observe()
    assert first.route == "/workspace"
    assert "Find a member" in first.headings
    assert {item.id for item in first.controls}.isdisjoint({item.id for item in second.controls})
    await select_member(surface)
    await click(surface, "Open profile")
    await click(surface, "View accounts")
    await click(surface, "Open savings")
    assert await surface.is_visible(target("Savings account", "heading"))
    assert await surface.extract(Target(kind="label", name="Current balance", frame="Member workspace")) == "4,250.75"
    snapshot = await surface.safe_snapshot()
    serialized = json.dumps(snapshot)
    assert snapshot["observation"]["route"] == "/workspace/savings"
    assert snapshot["structure"]["visible_frames"] == 1
    for secret in ["1001", "4,250.75", "Avery Example", "relay_demo_session"]:
        assert secret not in serialized
    assert "Savings account" in serialized


async def test_native_required_form_submit_appears_only_after_valid_fill(surface, demo_origin):
    await surface.start(demo_origin)
    frame = surface.page.frames[1]
    await frame.evaluate("""() => {
        window.observedInvalidEvents = 0;
        document.addEventListener('invalid', () => window.observedInvalidEvents++, true);
    }""")
    initial = await surface.observe()
    assert "Member ID" in {control.target.name for control in initial.controls}
    assert "Find member" not in {control.target.name for control in initial.controls}
    member = Target(kind="label", name="Member ID", frame="Member workspace")
    await surface.act(Step(action="fill", target=member, input_ref="member_id"), {"member_id": "PRIVATE-FORM-VALUE"})
    filled = await surface.observe()
    assert "Find member" in {control.target.name for control in filled.controls}
    assert "PRIVATE-FORM-VALUE" not in filled.model_dump_json()
    # Current DOM validity wins over a previously successful fill.
    await frame.get_by_label("Member ID", exact=True).fill("")
    cleared = await surface.observe()
    assert "Find member" not in {control.target.name for control in cleared.controls}
    assert await frame.evaluate("window.observedInvalidEvents") == 0


@pytest.mark.parametrize("bypass", ["form", "submitter"])
async def test_native_submit_honors_explicit_validation_bypass(surface, demo_origin, bypass):
    await surface.start(demo_origin)
    await surface.page.frames[1].get_by_role("button", name="Find member", exact=True).evaluate(
        "(el, bypass) => { if (bypass === 'form') el.form.noValidate = true; else el.formNoValidate = true; }", bypass)
    assert "Find member" in {control.target.name for control in (await surface.observe()).controls}


async def test_invalid_form_does_not_hide_links_non_submit_buttons_or_other_forms(surface, demo_origin):
    await surface.start(demo_origin)
    await surface.page.frames[1].evaluate("""() => {
        const form = document.querySelector('form');
        const link = document.createElement('a');
        link.href = '/workspace/profile'; link.textContent = 'Open profile'; form.append(link);
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.textContent = 'Retry'; form.append(cancel);
        const other = document.createElement('form');
        const submit = document.createElement('button');
        submit.type = 'submit'; submit.textContent = 'View accounts'; other.append(submit);
        document.body.append(other);
    }""")
    names = {control.target.name for control in (await surface.observe()).controls}
    assert "Find member" not in names
    assert {"Member ID", "Open profile", "Retry", "View accounts"} <= names


async def test_operator_recovery_preserves_the_same_live_browser(surface, demo_origin):
    await open_profile(surface, demo_origin, "session")
    original_page, original_session = surface.page, surface.session_id
    assert (await surface.observe()).condition == "session_expired"
    with pytest.raises(SurfaceError) as caught:
        await click(surface, "Restore session", "button")
    assert caught.value.code == "policy_blocked"
    await click(surface, "Restore session", "button", operator=True)
    assert (await surface.observe()).condition == "ready"
    assert surface.page is original_page and surface.session_id == original_session
    await click(surface, "View accounts")
    await click(surface, "Open savings")
    assert await surface.extract(Target(kind="label", name="Current balance", frame="Member workspace")) == "4,250.75"


async def test_transient_retry_uses_the_visible_recovery_control(surface, demo_origin):
    await open_profile(surface, demo_origin, "transient")
    assert (await surface.observe()).condition == "transient"
    await click(surface, "Retry", "button")
    assert (await surface.observe()).condition == "ready"
    assert await surface.is_visible(target("View accounts"))


@pytest.mark.parametrize("scenario,condition", [
    ("permission", "permission_denied"), ("error", "app_error"), ("dialog", "unexpected_dialog"),
])
async def test_visible_failure_conditions(surface, demo_origin, scenario, condition):
    await open_profile(surface, demo_origin, scenario)
    assert (await surface.observe()).condition == condition


async def test_not_found_is_distinct_from_unexpected_failure(surface, demo_origin):
    await surface.start(demo_origin)
    await select_member(surface, "9999")
    assert (await surface.observe()).condition == "not_found"
    assert not await surface.is_visible(target("Open profile"))


async def test_duplicate_link_never_selects_an_arbitrary_first_match(surface, demo_origin):
    await surface.start(demo_origin + "/?scenario=ambiguous")
    await select_member(surface)
    with pytest.raises(SurfaceError) as caught:
        await click(surface, "Open profile")
    assert caught.value.code == "ambiguous_target"
    with pytest.raises(SurfaceError) as caught:
        await surface.observe()
    assert caught.value.code == "ambiguous_target"


async def test_navigation_to_untrusted_origin_is_blocked_before_network(surface, demo_origin):
    await surface.start(demo_origin)
    await select_member(surface)
    frame = surface.page.frames[1]
    await frame.get_by_role("link", name="Open profile", exact=True).evaluate("el => el.href = 'https://example.invalid/private-secret'")
    with pytest.raises(SurfaceError) as caught:
        await click(surface, "Open profile")
    assert caught.value.code == "policy_blocked"
    assert "private-secret" not in str(caught.value)
    assert (await surface.observe()).condition == "policy_blocked"


async def test_unknown_js_dialog_is_dismissed_and_stops_without_echoing_text(surface, demo_origin):
    await surface.start(demo_origin)
    frame = surface.page.frames[1]
    await frame.get_by_role("button", name="Find member", exact=True).evaluate("el => {el.type = 'button'; el.onclick = () => confirm('secret-account-value');}")
    with pytest.raises(SurfaceError) as caught:
        await click(surface, "Find member", "button")
    assert caught.value.code == "unexpected_dialog"
    assert "secret-account-value" not in str(caught.value)
    assert (await surface.observe()).condition == "unexpected_dialog"


async def test_duplicate_iframe_title_is_not_arbitrarily_resolved(surface, demo_origin):
    await surface.start(demo_origin)
    await surface.page.evaluate("""() => new Promise(resolve => {
      const frame = document.createElement('iframe'); frame.title = 'Member workspace';
      frame.src = '/workspace'; frame.onload = resolve; document.body.append(frame);
    })""")
    with pytest.raises(SurfaceError) as caught:
        await surface.is_visible(Target(kind="label", name="Member ID", frame="Member workspace"))
    assert caught.value.code == "ambiguous_target"


async def test_unknown_page_content_and_parameter_never_enter_snapshot(surface, demo_origin):
    await surface.start(demo_origin)
    await surface.act(Step(action="fill", target=Target(kind="label", name="Member ID", frame="Member workspace"), input_ref="member_id"), {"member_id": "private-parameter"})
    await surface.page.frames[1].evaluate("""() => {
       const text = document.createElement('h2'); text.textContent = 'PRIVATE HISTORY: 999,999'; document.body.append(text);
       const button = document.createElement('button'); button.textContent = 'Delete private-parameter'; document.body.append(button);
    }""")
    serialized = json.dumps(await surface.safe_snapshot())
    assert "private-parameter" not in serialized
    assert "PRIVATE HISTORY" not in serialized
    assert "999,999" not in serialized


@pytest.mark.parametrize("intermediate", [False, True])
async def test_redirect_cannot_send_a_request_to_another_origin(intermediate):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"outside policy")

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class Redirector(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path in {"/workspace/profile", "/workspace/accounts"}:
                self.send_response(302)
                location = "/workspace/accounts" if intermediate and self.path == "/workspace/profile" else f"http://127.0.0.1:{destination.server_port}/leaked"
                self.send_header("Location", location)
                self.end_headers()
                return
            body = (b'<iframe title="Member workspace" src="/workspace"></iframe>' if self.path == "/" else b'<a href="/workspace/profile">Open profile</a>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    instance = Surface(Policy(origin=f"http://127.0.0.1:{source.server_port}"))
    try:
        await instance.start(instance.policy.origin)
        with pytest.raises(SurfaceError) as caught:
            await click(instance, "Open profile")
        assert caught.value.code == "policy_blocked"
        assert received == [], "The rejected redirect must never reach its destination"
    finally:
        await instance.close()
        source.shutdown()
        destination.shutdown()
        source_thread.join(timeout=3)
        destination_thread.join(timeout=3)
        source.server_close()
        destination.server_close()


async def test_popup_is_closed_and_stops_session(surface, demo_origin):
    await surface.start(demo_origin)
    frame = surface.page.frames[1]
    await frame.get_by_role("button", name="Find member", exact=True).evaluate("el => {el.type = 'button'; el.onclick = () => window.open('/workspace');}")
    with pytest.raises(SurfaceError) as caught:
        await click(surface, "Find member", "button")
    assert caught.value.code == "policy_blocked"
    assert (await surface.observe()).condition == "policy_blocked"
    assert len(surface.page.context.pages) == 1


async def test_websocket_cannot_open_outside_route_policy(surface, demo_origin):
    await surface.start(demo_origin)
    await surface.page.evaluate("""() => new Promise(resolve => {
       const socket = new WebSocket('ws://127.0.0.1:9/private');
       socket.onclose = resolve; socket.onerror = resolve;
    })""")
    assert (await surface.observe()).condition == "policy_blocked"
