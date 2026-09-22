"""Loopback-only operator console. It never connects to real banking software."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from .engine import Run
from .models import Capability, StrictModel
from .surface import SurfaceError


ROOT = Path(__file__).resolve().parents[1]
RUNS: dict[str, Run] = {}
TASKS: dict[str, asyncio.Task] = {}
LATEST = ROOT / "runtime" / "latest-capability.json"
STATIC = Path(__file__).parent / "static"
PAGES = {
    "configure": ("New run", "Give your workflow a direction.", "Discover a reusable workflow or replay a verified capability."),
    "execution": ("Execution", "Every action, accounted for.", "Follow the live session and take control when the workflow needs you."),
    "activity": ("Activity", "A clear record of every run.", "Inspect execution history, recovery, and ownership changes."),
    "workflows": ("Workflows", "Discover once. Replay with confidence.", "Inspect the typed capability that powers deterministic execution."),
}


@asynccontextmanager
async def lifespan(app):
    yield
    for run in RUNS.values():
        if run.result is None:
            await run.abort()
    for task in TASKS.values():
        if not task.done():
            task.cancel()
    await asyncio.gather(*TASKS.values(), return_exceptions=True)


app = FastAPI(title="Relay", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.middleware("http")
async def loopback_boundary(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in {"127.0.0.1:4310", "localhost:4310", "testserver"}:
        return JSONResponse({"detail": "Invalid console host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None and origin not in {"http://127.0.0.1:4310", "http://localhost:4310"}:
        return JSONResponse({"detail": "Cross-origin operator requests are forbidden"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
    return response


class StartRequest(StrictModel):
    mode: Literal["discover", "replay"]
    goal: str = Field(default="Find the current savings balance for the supplied member.", min_length=3, max_length=400)
    inputs: dict[str, str]
    scenario: Literal["normal", "not_found", "session", "transient", "permission", "error", "dialog", "slow", "ambiguous"] = "normal"


class OperatorRequest(StrictModel):
    action: Literal["restore_session", "retry"]


def find_run(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Run not found")
    return RUNS[run_id]


def load_capability() -> tuple[Capability | None, str | None]:
    """Load a validated artifact without leaking file contents or local paths."""
    path = LATEST if LATEST.exists() else ROOT / "evidence" / "capability.json"
    if not path.exists():
        return None, None
    try:
        capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        raise HTTPException(503, "The saved workflow is unavailable. Discover a new successful workflow to replace it.") from None
    return capability, "discovery" if path == LATEST else "example"


def summarize_run(run: Run) -> dict:
    """The workspace index intentionally omits goals, inputs and output values."""
    started_at = next((event["at"] for event in run.events if event["event"] == "run_started"), None)
    finished_at = next((event["at"] for event in reversed(run.events) if event["event"] == "run_finished"), None)
    return {"id": run.id, "mode": run.mode, "status": run.status, "owner": run.owner,
            "step": run.step, "model_calls": run.model_calls, "started_at": started_at,
            "finished_at": finished_at, "recoveries": run.recoveries, "handoffs": run.handoffs}


async def execute(run: Run):
    await run.execute()
    if run.mode == "discover" and run.status == "success":
        LATEST.parent.mkdir(exist_ok=True)
        LATEST.write_text(run.capability.model_dump_json(indent=2) + "\n")


@app.get("/")
async def home():
    return RedirectResponse("/configure", status_code=307)


@app.get("/configure", response_class=HTMLResponse)
@app.get("/execution", response_class=HTMLResponse)
@app.get("/activity", response_class=HTMLResponse)
@app.get("/workflows", response_class=HTMLResponse)
async def console_page(request: Request):
    page = request.url.path.lstrip("/")
    title, heading, subtitle = PAGES[page]
    values = {"PAGE": page, "TITLE": title, "HEADING": heading, "SUBTITLE": subtitle,
              "CONTENT": (STATIC / "pages" / f"{page}.html").read_text(encoding="utf-8")}
    document = (STATIC / "shell.html").read_text(encoding="utf-8")
    for key, value in values.items():
        document = document.replace(f"@@{key}@@", value)
    return HTMLResponse(document)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "relay", "synthetic_demo_only": True}


@app.get("/api/workspace")
async def workspace():
    runs = list(reversed(RUNS.values()))
    return {"active_run_id": next((run.id for run in runs if run.result is None), None),
            "latest_run_id": runs[0].id if runs else None,
            "runs": [summarize_run(run) for run in runs]}


@app.get("/api/capability")
async def get_capability():
    capability, source = load_capability()
    return {"capability": capability.model_dump() if capability else None, "source": source}


@app.post("/api/runs", status_code=202)
async def start(body: StartRequest):
    if any(run.result is None for run in RUNS.values()):
        raise HTTPException(409, "Finish or abort the active run first")
    capability = None
    if body.mode == "replay":
        capability, _ = load_capability()
        if capability is None:
            raise HTTPException(409, "Discover a successful flow before replaying")
    run = Run(mode=body.mode, goal=body.goal, inputs=body.inputs,
              target_url=f"http://127.0.0.1:4311/?scenario={body.scenario}",
              capability=capability, evidence_root=ROOT / "runtime")
    if len(RUNS) >= 30:
        oldest = next(iter(RUNS))
        del RUNS[oldest]
        TASKS.pop(oldest, None)
    RUNS[run.id] = run
    TASKS[run.id] = asyncio.create_task(execute(run))
    return {"id": run.id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    return find_run(run_id).public_state()


@app.post("/api/runs/{run_id}/claim")
async def claim(run_id: str):
    try:
        await find_run(run_id).claim()
    except ValueError:
        raise HTTPException(409, "Run is not awaiting operator control") from None
    return find_run(run_id).public_state()


@app.post("/api/runs/{run_id}/operator")
async def operator(run_id: str, body: OperatorRequest):
    try:
        await find_run(run_id).operator_action(body.action)
    except ValueError:
        raise HTTPException(409, "Claim operator control first") from None
    except SurfaceError as exc:
        raise HTTPException(409, exc.code) from None
    return find_run(run_id).public_state()


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str):
    try:
        await find_run(run_id).resume()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return find_run(run_id).public_state()


@app.post("/api/runs/{run_id}/abort")
async def abort(run_id: str):
    run = find_run(run_id)
    if run.result is None:
        await run.abort()
    return run.public_state()
