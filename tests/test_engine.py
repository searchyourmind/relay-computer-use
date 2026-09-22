"""Engine regressions: real UI replay and explicitly fake planner boundary tests.

The capability below is a test fixture, not evidence of model discovery. Browser
tests launch an isolated local demo server; no external model or account is used.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from pydantic import ValidationError

from relaycu import engine
from relaycu.demo import app
from relaycu.engine import CHECKPOINT, FRAME, INPUTS, OUTPUTS, Run
from relaycu.models import Capability, Decision, Observation, Step, Target
from relaycu.surface import Policy, Surface


@pytest.fixture(scope="module")
def demo_url():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Synthetic UI server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


@pytest.fixture
def capability():
    steps = [Step(action="fill", target=Target(kind="label", name="Member ID", frame=FRAME), input_ref="member_id")]
    for role, name in [("button", "Find member"), ("link", "Open profile"), ("link", "View accounts"), ("link", "Open savings")]:
        steps.append(Step(action="click", target=Target(kind="role", role=role, name=name, frame=FRAME)))
    return Capability(name="test_read_savings_balance", inputs=INPUTS, outputs=OUTPUTS, steps=steps, checkpoint=CHECKPOINT)


class ExplodingPlanner:
    """Any attempted provider access fails, including just reading its calls."""

    def __getattribute__(self, name):
        raise AssertionError(f"Replay accessed the model provider: {name}")


def replay(tmp_path, capability, demo_url, *, member="1002", scenario="normal", **options):
    return Run(
        mode="replay", goal="Read the savings balance", inputs={"member_id": member},
        target_url=f"{demo_url}/?scenario={scenario}", capability=capability,
        surface=Surface(Policy(origin=demo_url)), planner=ExplodingPlanner(),
        evidence_root=tmp_path, deadline_seconds=45, operator_timeout=15, **options,
    )


async def await_operator(run, task):
    async with asyncio.timeout(25):
        while run.status != "awaiting_operator":
            if task.done():
                pytest.fail(f"Run finished before handoff: {task.result()}")
            await asyncio.sleep(0.01)


def evidence_text(run):
    return "\n".join(path.read_text() for path in run.evidence_dir.rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_replay_reparameterizes_without_constructing_or_calling_model(tmp_path, capability, demo_url, monkeypatch):
    def forbidden_constructor(*args, **kwargs):
        raise AssertionError("Replay constructed a planner")

    monkeypatch.setattr(engine, "OllamaPlanner", forbidden_constructor)
    run = replay(tmp_path, capability, demo_url)
    result = await run.execute()
    assert result["status"] == "success", result
    assert result["outputs"] == {"current_balance": "9180.20"}
    assert result["model_calls"] == 0
    assert run.planner is None
    assert run.surface.page is None
    persisted = evidence_text(run)
    assert "9180.20" not in persisted and "9,180.20" not in persisted
    assert "[returned to caller; not persisted]" in persisted


@pytest.mark.asyncio
async def test_not_found_is_business_outcome_and_sensitive_input_never_persists(tmp_path, capability, demo_url):
    sentinel = "PRIVATE_7fbd2ca910"
    run = replay(tmp_path, capability, demo_url, member=sentinel, scenario="not_found")
    result = await run.execute()
    assert result["status"] == "business_outcome", result
    assert result["code"] == "not_found"
    assert result["model_calls"] == 0
    assert result["handoffs"] == 0
    assert sentinel not in evidence_text(run)
    assert sentinel not in json.dumps(run.events)
    assert list(run.evidence_dir.glob("state-*.json"))


@pytest.mark.asyncio
async def test_transient_ui_recovers_without_model(tmp_path, capability, demo_url):
    run = replay(tmp_path, capability, demo_url, scenario="transient")
    result = await run.execute()
    assert result["status"] == "success"
    assert result["outputs"] == {"current_balance": "9180.20"}
    assert result["recoveries"] == 1
    assert result["handoffs"] == result["model_calls"] == 0
    assert [event["attempt"] for event in run.events if event["event"] == "bounded_recovery"] == [1]


@pytest.mark.asyncio
async def test_session_handoff_restores_same_browser_and_continues_from_saved_step(tmp_path, capability, demo_url):
    run = replay(tmp_path, capability, demo_url, scenario="session")
    task = asyncio.create_task(run.execute())
    try:
        await await_operator(run, task)
        session_id, pointer = run.surface.session_id, run.step
        assert run.intervention["reason"] == "session_expired"
        assert pointer == 3
        with pytest.raises(ValueError, match="claim control"):
            await run.operator_action("restore_session")
        await run.claim()
        with pytest.raises(ValueError, match="Resolve"):
            await run.resume()
        await run.operator_action("restore_session")
        assert run.observation.condition == "ready"
        assert run.step == pointer
        assert run.surface.session_id == session_id
        await run.resume()
        result = await task
        assert result["status"] == "success"
        assert result["outputs"] == {"current_balance": "9180.20"}
        assert result["handoffs"] == 1 and result["model_calls"] == 0
        assert run.surface.session_id == session_id
        assert [e["action"] for e in run.events if e["event"] == "operator_action"] == ["restore_session"]
        assert [e["owner"] for e in run.events if e["event"] == "control_transferred"] == ["human", "automation"]
        completed = [e for e in run.events if e["event"] == "action_completed"]
        assert [e["step"] for e in completed] == list(range(5))
        assert len(list(run.evidence_dir.glob("failure-step-*.json"))) == 1
    finally:
        if not task.done():
            await run.abort()
            await task


class FakeSurface:
    """Minimal boundary fake; never presented as browser or discovery evidence."""

    def __init__(self, condition="ready", *, start_delay=0):
        self.session_id = "test-fake-browser"
        self.condition = condition
        self.start_delay = start_delay
        self.started = self.closed = False
        self.actions = []

    async def start(self, *_args, **_kwargs):
        self.started = True
        await asyncio.sleep(self.start_delay)

    async def close(self):
        self.closed = True

    async def observe(self):
        return Observation(route="/workspace", headings=[], controls=[], condition=self.condition)

    async def safe_snapshot(self):
        return {"observation": (await self.observe()).model_dump()}

    async def act(self, step, params):
        self.actions.append(step.target.name)

    async def is_visible(self, target):
        return False


@pytest.mark.asyncio
async def test_persistent_transient_stops_at_two_recoveries_and_can_be_aborted(tmp_path, capability):
    surface = FakeSurface("transient")
    run = Run(mode="replay", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              capability=capability, surface=surface, evidence_root=tmp_path, operator_timeout=1)
    task = asyncio.create_task(run.execute())
    await await_operator(run, task)
    assert surface.actions == ["Retry", "Retry"]
    assert run.recoveries == 2 and run.intervention["reason"] == "transient"
    await run.claim()
    await run.abort()
    result = await task
    assert result["code"] == "operator_aborted"
    assert result["model_calls"] == 0 and surface.closed


@pytest.mark.asyncio
async def test_operator_timeout_is_a_terminal_audited_failure(tmp_path, capability):
    surface = FakeSurface("session_expired")
    sentinel = "PRIVATE_OPERATOR_823"
    run = Run(mode="replay", goal="test", inputs={"member_id": sentinel}, target_url="http://unused/",
              capability=capability, surface=surface, evidence_root=tmp_path, operator_timeout=0.02)
    result = await run.execute()
    assert result["status"] == "failure" and result["code"] == "operator_timeout"
    assert surface.closed
    assert any(e["event"] == "intervention_requested" for e in run.events)
    assert list(run.evidence_dir.glob("failure-step-*.json"))
    assert sentinel not in evidence_text(run)


@pytest.mark.asyncio
async def test_total_deadline_also_bounds_replay_browser_start(tmp_path, capability):
    surface = FakeSurface(start_delay=1)
    run = Run(mode="replay", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              capability=capability, surface=surface, evidence_root=tmp_path, deadline_seconds=0.02)
    result = await run.execute()
    assert result["code"] == "run_timeout" and surface.closed
    assert not surface.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [{}, {"member_id": 1002}, {"member_id": ""}, {"member_id": "x" * 25},
                                      {"member_id": "1002\n"}, {"member_id": "1002", "extra": "value"}])
async def test_invalid_inputs_fail_before_starting_browser(tmp_path, capability, inputs):
    surface = FakeSurface()
    run = Run(mode="replay", goal="test", inputs=inputs, target_url="http://unused/", capability=capability,
              surface=surface, evidence_root=tmp_path)
    result = await run.execute()
    assert result["code"] == "invalid_inputs"
    assert not surface.started
    assert not any(event["event"] == "session_opened" for event in run.events)


class ForgedCompletionPlanner:
    """Intentionally dishonest test double, not a real model invocation."""

    calls = 0
    last_metrics = {}

    async def decide(self, *_args):
        self.calls += 1
        return Decision(action="done", control=None, input_ref=None, reason="complete")


@pytest.mark.asyncio
async def test_forged_model_completion_requires_browser_checkpoint(tmp_path):
    planner, surface = ForgedCompletionPlanner(), FakeSurface()
    run = Run(mode="discover", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              surface=surface, planner=planner, evidence_root=tmp_path)
    result = await run.execute()
    assert result["status"] == "failure" and result["code"] == "false_completion"
    assert planner.calls == 1 and surface.closed
    assert run.capability is None
    assert not (run.evidence_dir / "capability.json").exists()


@pytest.mark.parametrize("case", ["schema_version", "product_version", "extra", "reference", "revision", "action", "literal_value"])
def test_capability_contract_rejects_incompatible_or_unsafe_artifacts(capability, case):
    artifact = capability.model_dump()
    if case in {"schema_version", "product_version"}:
        artifact[case] = "999"
    elif case == "extra":
        artifact["hidden_script"] = "evaluate arbitrary javascript"
    elif case == "reference":
        artifact["steps"][0]["input_ref"] = "undeclared_member"
    elif case == "revision":
        artifact["revision"] = "1"
    elif case == "action":
        artifact["steps"][0]["action"] = "evaluate"
    else:
        artifact["steps"][0]["value"] = "PRIVATE_LITERAL"
    with pytest.raises(ValidationError):
        Capability.model_validate(artifact)


class GatedSurface(FakeSurface):
    """Boundary fake whose pending work can be cancelled before taking effect."""

    def __init__(self, gate_at):
        super().__init__()
        self.gate_at = gate_at
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.extracted = False

    async def gate(self, operation):
        if operation == self.gate_at:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def observe(self):
        from relaycu.models import Control

        return Observation(route="/workspace", headings=[], controls=[
            Control(id="member", target=Target(kind="label", name="Member ID", frame=FRAME), actions=["fill"]),
        ])

    async def act(self, step, params):
        await self.gate("action")
        self.actions.append(step.target.name)

    async def is_visible(self, target):
        return self.gate_at == "extraction"

    async def extract(self, target):
        await self.gate("extraction")
        self.extracted = True
        return "9180.20"


class GatedPlanner:
    calls = 0
    last_metrics = {}

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def decide(self, *_args):
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Decision(action="fill", control="member", input_ref="member_id", reason="locate_record")


@pytest.mark.asyncio
async def test_abort_cancels_pending_model_before_any_ui_action(tmp_path):
    surface, planner = GatedSurface("never"), GatedPlanner()
    run = Run(mode="discover", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              surface=surface, planner=planner, evidence_root=tmp_path)
    task = asyncio.create_task(run.execute())
    await asyncio.wait_for(planner.entered.wait(), timeout=1)
    await asyncio.wait_for(asyncio.gather(run.abort(), run.abort()), timeout=1)
    assert task.done() and planner.cancelled
    assert run.result["code"] == "operator_aborted"
    assert surface.closed and not surface.actions
    assert [e["event"] for e in run.events].count("abort_requested") == 1
    assert [e["event"] for e in run.events].count("run_finished") == 1
    planner.release.set()
    await asyncio.sleep(0)
    assert not surface.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["action", "extraction"])
async def test_abort_cancels_pending_browser_work_and_never_reports_success(tmp_path, capability, pending):
    surface = GatedSurface(pending)
    run = Run(mode="replay", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              capability=capability, surface=surface, evidence_root=tmp_path)
    task = asyncio.create_task(run.execute())
    await asyncio.wait_for(surface.entered.wait(), timeout=1)
    await asyncio.wait_for(run.abort(), timeout=1)
    assert task.done() and surface.cancelled and surface.closed
    assert run.result["status"] == "failure" and run.result["code"] == "operator_aborted"
    assert run.result["outputs"] == {} and not surface.extracted
    if pending == "action":
        assert not surface.actions
    surface.release.set()
    await asyncio.sleep(0)
    assert run.result["status"] == "failure"
    assert [e["event"] for e in run.events].count("run_finished") == 1


@pytest.mark.asyncio
async def test_abort_before_execution_never_opens_browser(tmp_path, capability):
    surface = FakeSurface()
    run = Run(mode="replay", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              capability=capability, surface=surface, evidence_root=tmp_path)
    await run.abort()
    result = await run.execute()
    assert result["code"] == "operator_aborted" and not surface.started
    assert [e["event"] for e in run.events].count("run_finished") == 1


@pytest.mark.asyncio
async def test_failure_diagnostics_describe_expected_and_observed_safe_state(tmp_path, capability):
    surface = FakeSurface("session_expired")
    run = Run(mode="replay", goal="PRIVATE_GOAL_817", inputs={"member_id": "PRIVATE_INPUT_816"},
              target_url="http://unused/", capability=capability, surface=surface,
              evidence_root=tmp_path, operator_timeout=0.01)
    result = await run.execute()
    diagnostics = result["diagnostics"]
    assert diagnostics["step"] == 0
    assert diagnostics["expected"]["state"] == "operator resolution before the handoff deadline"
    assert diagnostics["expected"]["action"] == "fill"
    assert diagnostics["expected"]["target"] == capability.steps[0].target.model_dump()
    assert diagnostics["observed"] == {"condition": "session_expired", "route": "/workspace"}
    persisted = json.loads((run.evidence_dir / "result.json").read_text())
    assert persisted["diagnostics"] == diagnostics
    for sentinel in ["PRIVATE_GOAL_817", "PRIVATE_INPUT_816"]:
        assert sentinel not in evidence_text(run)


@pytest.mark.asyncio
async def test_untrusted_artifact_target_and_raw_error_do_not_enter_failure_diagnostics(tmp_path, capability):
    from relaycu.surface import SurfaceError

    sentinel = "PRIVATE_ARTIFACT_SELECTOR_815"
    raw_error = "PRIVATE_BROWSER_ERROR_814"
    artifact = capability.model_copy(deep=True)
    artifact.steps[0].target.name = sentinel

    class RejectedTargetSurface(FakeSurface):
        async def act(self, *_args):
            raise SurfaceError("policy_blocked", raw_error)

    run = Run(mode="replay", goal="test", inputs={"member_id": "1002"}, target_url="http://unused/",
              capability=artifact, surface=RejectedTargetSurface(), evidence_root=tmp_path,
              operator_timeout=0.01)
    result = await run.execute()
    assert result["status"] == "failure"
    assert "target" not in result["diagnostics"]["expected"]
    assert sentinel not in evidence_text(run) and raw_error not in evidence_text(run)
