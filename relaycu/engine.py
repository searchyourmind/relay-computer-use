from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Capability, InputSpec, Observation, OutputSpec, Step, Target, validate_inputs
from .provider import ModelError, OllamaPlanner
from .surface import Policy, Surface, SurfaceError


FRAME = "Member workspace"
CHECKPOINT = Target(kind="role", role="heading", name="Savings account", frame=FRAME)
INPUTS = {"member_id": InputSpec(description="Synthetic member identifier; provided at invocation")}
OUTPUTS = {"current_balance": OutputSpec(type="decimal", target=Target(kind="label", name="Current balance", frame=FRAME), description="Savings balance in USD, exact decimal string")}


class Run:
    """One owner, one browser session, one monotonic continuation pointer.

    Persisted evidence deliberately excludes goal text, input values, extracted
    outputs, full DOM, screenshots, cookies and raw model transcripts.
    """

    def __init__(self, *, mode: str, goal: str, inputs: dict, target_url: str,
                 capability: Capability | None = None, surface: Surface | None = None,
                 planner: OllamaPlanner | None = None, evidence_root: Path | None = None,
                 max_steps: int = 16, deadline_seconds: int = 300,
                 operator_timeout: int = 300, headed: bool = False):
        if mode not in {"discover", "replay"}:
            raise ValueError("Unknown run mode")
        if mode == "replay" and capability is None:
            raise ValueError("Replay requires a validated capability")
        self.id = uuid.uuid4().hex
        self.mode, self.goal, self.inputs = mode, goal, inputs
        self.target_url, self.capability = target_url, capability
        self.surface = surface or Surface()
        # Replay has no planner reference even when a caller passes one.
        self.planner = (planner or OllamaPlanner()) if mode == "discover" else None
        self.evidence_dir = (evidence_root or Path("runtime")) / self.id
        self.max_steps, self.deadline_seconds = max_steps, deadline_seconds
        self.operator_timeout, self.headed = operator_timeout, headed
        self.status, self.owner = "running", "automation"
        self.step = 0
        self.events: list[dict] = []
        self.result: dict | None = None
        self.intervention: dict | None = None
        self.observation: Observation | None = None
        self.recorded: list[Step] = []
        self.resume_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.aborted = False
        self.recoveries = 0
        self.handoffs = 0
        self.current_step: Step | None = None
        self._execution_task: asyncio.Task | None = None

    @property
    def model_calls(self):
        return self.planner.calls if self.planner else 0

    def log(self, event: str, **data):
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, "step": self.step, **data}
        self.events.append(row)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with (self.evidence_dir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def public_state(self):
        return {"id": self.id, "mode": self.mode, "status": self.status, "owner": self.owner,
                "step": self.step, "model_calls": self.model_calls,
                "session_id": self.surface.session_id,
                "events": self.events, "result": self.result,
                "capability": self.capability.model_dump() if self.capability else None,
                "intervention": self.intervention,
                "observation": self.observation.model_dump() if self.observation else None}

    def failure_diagnostics(self, code: str) -> dict:
        """Explain the failed contract using executor-owned vocabulary only."""
        policy = getattr(self.surface, "policy", None) or Policy()
        states = {
            "invalid_inputs": "valid parameters matching the declared input contract",
            "checkpoint_failed": "the declared success checkpoint is visible",
            "false_completion": "the declared success checkpoint is visible",
            "output_contract": "an output matching the declared type and bounds",
            "operator_timeout": "operator resolution before the handoff deadline",
            "operator_aborted": "execution stopped at the operator's request",
            "run_cancelled": "execution completed before its task was cancelled",
            "run_timeout": "completion within the overall run deadline",
            "model_unavailable_or_invalid": "a valid structured decision from the model",
            "step_limit": "the success checkpoint within the allowed step count",
        }
        expected = {"state": states.get(code, "a ready application and an unambiguous permitted control")}
        target = None
        if code in {"checkpoint_failed", "false_completion"}:
            target = self.capability.checkpoint if self.capability else CHECKPOINT
        elif self.current_step is not None:
            expected["action"] = self.current_step.action
            target = self.current_step.target
        # An artifact is untrusted. In particular, its selector name must not be
        # echoed into diagnostics before it has passed the trusted vocabulary.
        if target is not None and (target.frame is None or target.frame in policy.frame_titles):
            permitted = (
                target.kind == "label" and target.name in policy.fill_labels | policy.extract_labels
                or target.kind == "role" and target.role == "heading" and target.name in policy.heading_names
                or target.kind == "role" and target.role in {"button", "link"}
                and target.name in policy.click_names | policy.operator_click_names
            )
            if permitted:
                expected["target"] = target.model_dump()
        observation = self.observation
        return {"step": self.step, "expected": expected, "observed": {
            "condition": observation.condition if observation else "unavailable",
            "route": observation.route if observation and observation.route in policy.routes else "[unavailable]",
        }}

    def finish(self, status: str, code: str, outputs: dict | None = None):
        if self.result is not None:
            return
        if self.aborted:
            status, code, outputs = "failure", "operator_aborted", None
        self.status, self.owner = status, "finished"
        self.result = {"status": status, "code": code, "step": self.step,
                       "outputs": outputs or {}, "model_calls": self.model_calls,
                       "recoveries": self.recoveries, "handoffs": self.handoffs}
        if status == "failure":
            self.result["diagnostics"] = self.failure_diagnostics(code)
        # Outputs are returned to the caller; evidence records shape only.
        self.log("run_finished", status=status, code=code, model_calls=self.model_calls,
                 output_fields=sorted((outputs or {}).keys()), recoveries=self.recoveries, handoffs=self.handoffs)
        safe_result = {**self.result, "outputs": {key: "[returned to caller; not persisted]" for key in (outputs or {})}}
        (self.evidence_dir / "result.json").write_text(json.dumps(safe_result, indent=2) + "\n")

    async def escalate(self, reason: str, wait_seconds: float | None = None) -> bool:
        self.handoffs += 1
        if self.handoffs > 3:
            self.finish("failure", "handoff_limit")
            return False
        try:
            self.observation = await self.surface.observe()
            snapshot = await self.surface.safe_snapshot()
        except SurfaceError:
            self.observation = Observation(route="[unavailable]", headings=[], controls=[], condition="app_error")
            snapshot = {"observation": self.observation.model_dump(), "capture_status": "surface_unavailable", "failure_code": reason}
        (self.evidence_dir / f"failure-step-{self.step}.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        self.resume_event.clear()
        self.status, self.owner = "awaiting_operator", "awaiting_operator"
        self.intervention = {"reason": reason, "step": self.step,
                             "observation": self.observation.model_dump(), "session_id": self.surface.session_id}
        self.log("intervention_requested", code=reason, session_id=self.surface.session_id,
                 evidence=f"failure-step-{self.step}.json", expected="ready application state",
                 observed=self.observation.condition)
        try:
            await asyncio.wait_for(self.resume_event.wait(), timeout=min(self.operator_timeout, wait_seconds) if wait_seconds is not None else self.operator_timeout)
        except TimeoutError:
            self.finish("failure", "operator_timeout")
            return False
        if self.aborted:
            self.finish("failure", "operator_aborted")
            return False
        return True

    async def claim(self):
        async with self.lock:
            if self.owner != "awaiting_operator":
                raise ValueError("Run is not awaiting operator control")
            self.owner, self.status = "human", "human_control"
            self.log("control_transferred", owner="human", session_id=self.surface.session_id)

    async def operator_action(self, action: str):
        async with self.lock:
            if self.owner != "human":
                raise ValueError("Operator must claim control first")
            if action not in {"restore_session", "retry"}:
                raise ValueError("Operator action is not permitted")
            name = "Restore session" if action == "restore_session" else "Retry"
            step = Step(action="click", target=Target(kind="role", role="button", name=name, frame=FRAME))
            await self.surface.operator_act(step, {})
            self.observation = await self.surface.observe()
            if self.intervention:
                self.intervention["observation"] = self.observation.model_dump()
            self.log("operator_action", action=action, session_id=self.surface.session_id,
                     observed=self.observation.condition)

    async def resume(self):
        async with self.lock:
            if self.owner != "human":
                raise ValueError("Only the current operator can return control")
            self.observation = await self.surface.observe()
            if self.observation.condition != "ready":
                raise ValueError("Resolve the exceptional state before resuming")
            self.owner, self.status = "automation", "running"
            self.log("control_transferred", owner="automation", session_id=self.surface.session_id)
            self.intervention = None
            self.resume_event.set()

    async def abort(self):
        async with self.lock:
            if self.result is not None or self.aborted:
                task = self._execution_task
            else:
                self.aborted = True
                self.log("abort_requested")
                self.resume_event.set()
                task = self._execution_task
                if task is not None and task is not asyncio.current_task():
                    # Interrupt provider/browser awaits immediately. A flag checked
                    # only between steps lets a cancelled decision still act.
                    task.cancel()
                elif task is None:
                    self.finish("failure", "operator_aborted")
        if task is not None and task is not asyncio.current_task():
            # The caller gets the terminal result only after the browser closes.
            await asyncio.shield(task)

    async def handle_state(self) -> bool:
        self.observation = await self.surface.observe()
        # These observations contain only trusted label vocabulary and structure.
        (self.evidence_dir / f"state-{self.step}.json").write_text(json.dumps(self.observation.model_dump(), indent=2) + "\n")
        state = self.observation.condition
        if state == "ready":
            return True
        if state in {"not_found", "validation"}:
            self.finish("business_outcome", state)
            return False
        if state == "transient" and self.recoveries < 2:
            self.recoveries += 1
            self.log("bounded_recovery", code="transient", attempt=self.recoveries)
            await self.surface.act(Step(action="click", target=Target(kind="role", role="button", name="Retry", frame=FRAME)), {})
            return await self.handle_state()
        if state in {"session_expired", "unexpected_dialog", "permission_denied", "app_error", "transient", "policy_blocked"}:
            if not await self.escalate(state):
                return False
            return await self.handle_state()
        self.finish("failure", "unknown_state")
        return False

    async def outputs(self, outputs: dict[str, OutputSpec]) -> dict:
        result = {}
        for name, spec in outputs.items():
            raw = await self.surface.extract(spec.target)
            if spec.type == "decimal":
                try:
                    amount = Decimal(raw.strip().replace(",", "").replace("$", ""))
                    if not amount.is_finite() or amount.as_tuple().exponent < -2:
                        raise ValueError()
                    result[name] = format(amount, ".2f")
                except (InvalidOperation, ValueError):
                    raise SurfaceError("output_contract", "Output is not a valid monetary amount") from None
            else:
                if not raw.strip() or len(raw) > 500:
                    raise SurfaceError("output_contract", "Output is empty or exceeds the contract bound")
                result[name] = raw.strip()
        return result

    async def execute(self):
        if self.result is not None:
            return self.result
        self._execution_task = asyncio.current_task()
        try:
            await self._execute()
        except asyncio.CancelledError:
            self.finish("failure", "operator_aborted" if self.aborted else "run_cancelled")
            if not self.aborted:
                raise
        finally:
            await self.surface.close()
            self._execution_task = None
        return self.result

    async def _execute(self):
        specs = self.capability.inputs if self.capability else INPUTS
        try:
            validate_inputs(specs, self.inputs)
        except ValueError:
            self.finish("failure", "invalid_inputs")
            return
        if self.aborted:
            self.finish("failure", "operator_aborted")
            return
        self.log("run_started", mode=self.mode, input_fields=sorted(self.inputs), provider="ollama" if self.planner else None)
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.deadline_seconds):
                await self.surface.start(self.target_url, headed=self.headed)
                self.log("session_opened", session_id=self.surface.session_id)
                if self.mode == "replay":
                    await self.replay()
                else:
                    await self.discover(started)
        except SurfaceError as exc:
            self.log("execution_stopped", code=exc.code, expected="unambiguous permitted control")
            try:
                # Selector/policy failures never cause a blind retry or a skipped step.
                remaining = max(0.01, self.deadline_seconds - (time.monotonic() - started))
                await self.escalate(exc.code, wait_seconds=remaining)
            except Exception:
                pass
            if self.result is None:
                self.finish("failure", exc.code)
        except ModelError:
            self.finish("failure", "model_unavailable_or_invalid")
        except TimeoutError:
            self.finish("failure", "run_timeout")
        except Exception:
            self.finish("failure", "internal_error")

    async def replay(self):
        assert self.capability is not None and self.planner is None
        for index, step in enumerate(self.capability.steps):
            self.step = index
            self.current_step = step
            if self.aborted:
                self.finish("failure", "operator_aborted")
                return
            if not await self.handle_state():
                return
            await self.surface.act(step, self.inputs)
            self.log("action_completed", action=step.action, target=step.target.model_dump(), input_ref=step.input_ref)
        self.step = len(self.capability.steps)
        self.current_step = None
        if not await self.handle_state():
            return
        if not await self.surface.is_visible(self.capability.checkpoint):
            self.finish("failure", "checkpoint_failed")
            return
        values = await self.outputs(self.capability.outputs)
        self.log("checkpoint_verified", target=self.capability.checkpoint.model_dump())
        self.finish("success", "verified", values)

    async def discover(self, started: float):
        for index in range(self.max_steps):
            self.step = index
            self.current_step = None
            if self.aborted:
                self.finish("failure", "operator_aborted")
                return
            if time.monotonic() - started > self.deadline_seconds:
                self.finish("failure", "run_timeout")
                return
            if not await self.handle_state():
                return
            # The declared contract, not another model opinion, decides success.
            if self.recorded and await self.surface.is_visible(CHECKPOINT):
                await self.complete_discovery()
                return
            history = [step.model_dump() for step in self.recorded]
            decision = await self.planner.decide(self.goal, self.observation, history, list(INPUTS), CHECKPOINT.model_dump())
            self.log("model_decision", action=decision.action, control=decision.control,
                     input_ref=decision.input_ref, reason_code=decision.reason, **self.planner.last_metrics)
            if decision.action == "done":
                if not await self.surface.is_visible(CHECKPOINT):
                    self.finish("failure", "false_completion")
                    return
                await self.complete_discovery()
                return
            if decision.action == "escalate":
                if not await self.escalate("model_stuck"):
                    return
                continue
            control = next((c for c in self.observation.controls if c.id == decision.control), None)
            if control is None or decision.action not in control.actions:
                self.finish("failure", "invalid_model_action")
                return
            step = Step(action=decision.action, target=control.target, input_ref=decision.input_ref)
            self.current_step = step
            if step.input_ref is not None and step.input_ref not in INPUTS:
                self.finish("failure", "undeclared_input")
                return
            if len(self.recorded) >= 2 and self.recorded[-2:] == [step, step]:
                self.finish("failure", "repeated_action")
                return
            await self.surface.act(step, self.inputs)
            self.recorded.append(step)
            self.log("action_completed", action=step.action, target=step.target.model_dump(), input_ref=step.input_ref)
        self.finish("failure", "step_limit")

    async def complete_discovery(self):
        values = await self.outputs(OUTPUTS)
        self.capability = Capability(name="read_savings_balance", inputs=INPUTS, outputs=OUTPUTS,
                                     steps=self.recorded, checkpoint=CHECKPOINT)
        (self.evidence_dir / "capability.json").write_text(self.capability.model_dump_json(indent=2) + "\n")
        self.log("checkpoint_verified", target=CHECKPOINT.model_dump())
        self.finish("success", "verified", values)
