"""Local operator console with an opt-in, isolated public synthetic demo."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import shutil
import time
from collections import deque
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
# Hosted mode is deliberately a bounded synthetic demonstration, not a general
# multi-tenant control plane. All state is process-local; run one web worker.
HOSTED = os.getenv("RELAY_HOSTED", "0") == "1"
PUBLIC_HOST = os.getenv("RELAY_PUBLIC_HOST", os.getenv("RAILWAY_PUBLIC_DOMAIN", "")).strip().lower()
DISCOVERY_ENABLED = not HOSTED or os.getenv("RELAY_ENABLE_DISCOVERY", "0") == "1"
FIXED_GOAL = "Find the current savings balance for the supplied member."
COOKIE_NAME = "__Host-relay_session"
COOKIE_SECONDS = 8 * 60 * 60
SESSION_SECRET = secrets.token_bytes(32)
RUN_OWNERS: dict[str, str] = {}
START_TIMES: dict[str, deque] = {}
GLOBAL_START_TIMES: deque = deque()
RATE_WINDOW_SECONDS = 600
VISITOR_START_LIMIT = 6
GLOBAL_START_LIMIT = 30
MAX_HOSTED_CONCURRENT = 1
HOSTED_RUN_SECONDS = 180
HOSTED_OPERATOR_SECONDS = 90
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


def valid_public_host() -> bool:
    # Exact host matching. Configuration containing schemes, paths, userinfo or
    # wildcard suffixes fails closed rather than widening the origin boundary.
    return bool(PUBLIC_HOST and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", PUBLIC_HOST))


def session_token(now: int | None = None) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(32)
    payload = f"{nonce}.{int(time.time()) if now is None else now}"
    signature = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return nonce, f"{payload}.{signature}"


def validate_session(token: str | None) -> str | None:
    if not token or len(token) > 160:
        return None
    match = re.fullmatch(r"([A-Za-z0-9_-]{43})\.([0-9]{1,12})\.([0-9a-f]{64})", token)
    if not match:
        return None
    nonce, issued, signature = match.groups()
    age = time.time() - int(issued)
    if age < 0 or age > COOKIE_SECONDS:
        return None
    expected = hmac.new(SESSION_SECRET, f"{nonce}.{issued}".encode(), hashlib.sha256).hexdigest()
    return nonce if hmac.compare_digest(expected, signature) else None


@app.middleware("http")
async def loopback_boundary(request: Request, call_next):
    host = request.headers.get("host", "").lower()
    if HOSTED:
        # Railway's health checker supplies its own documented Host. This narrow
        # exception has no session or access to any workspace or mutation route.
        health_probe = request.method == "GET" and request.url.path == "/api/health" and host == "healthcheck.railway.app"
        if not valid_public_host() or (host != PUBLIC_HOST and not health_probe):
            return JSONResponse({"detail": "Invalid console host"}, status_code=403)
        allowed_origins = {f"https://{PUBLIC_HOST}"}
    else:
        if host not in {"127.0.0.1:4310", "localhost:4310", "testserver"}:
            return JSONResponse({"detail": "Invalid console host"}, status_code=403)
        allowed_origins = {"http://127.0.0.1:4310", "http://localhost:4310"}
    origin = request.headers.get("origin")
    if (origin is not None and origin not in allowed_origins) or (
        HOSTED and request.method not in {"GET", "HEAD", "OPTIONS"} and origin is None
    ):
        return JSONResponse({"detail": "Cross-origin operator requests are forbidden"}, status_code=403)
    if HOSTED and request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site console requests are forbidden"}, status_code=403)
    new_cookie = None
    request.state.visitor = None
    if HOSTED and request.url.path != "/api/health":
        visitor = validate_session(request.cookies.get(COOKIE_NAME))
        if visitor is None:
            visitor, new_cookie = session_token()
        request.state.visitor = visitor
    response = await call_next(request)
    if new_cookie:
        response.set_cookie(COOKIE_NAME, new_cookie, max_age=COOKIE_SECONDS, path="/",
                            secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
    if HOSTED:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


class StartRequest(StrictModel):
    mode: Literal["discover", "replay"]
    goal: str = Field(default=FIXED_GOAL, min_length=3, max_length=400)
    inputs: dict[str, str]
    scenario: Literal["normal", "not_found", "session", "transient", "permission", "error", "dialog", "slow", "ambiguous"] = "normal"


class OperatorRequest(StrictModel):
    action: Literal["restore_session", "retry"]


def find_run(run_id: str, request: Request):
    if run_id not in RUNS or (HOSTED and RUN_OWNERS.get(run_id) != request.state.visitor):
        raise HTTPException(404, "Run not found")
    return RUNS[run_id]


def load_capability(request: Request | None = None) -> tuple[Capability | None, str | None]:
    """Load a validated artifact without leaking file contents or local paths."""
    if HOSTED:
        # A discovery belongs to its visitor; another visitor cannot replace the
        # workflow used for your next run or inspect your discovered artifact.
        visitor = request.state.visitor if request is not None else None
        for run in reversed(RUNS.values()):
            if visitor is not None and RUN_OWNERS.get(run.id) == visitor and run.mode == "discover" and run.status == "success":
                return run.capability, "discovery"
        path = ROOT / "evidence" / "capability.json"
    else:
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
    if not HOSTED and run.mode == "discover" and run.status == "success":
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


@app.get("/api/config")
async def configuration():
    return {"hosted": HOSTED, "discovery_enabled": DISCOVERY_ENABLED,
            "fixed_goal": FIXED_GOAL if HOSTED else None,
            "member_ids": ["1001", "1002"] if HOSTED else None,
            "run_deadline_seconds": HOSTED_RUN_SECONDS if HOSTED else 300,
            "operator_timeout_seconds": HOSTED_OPERATOR_SECONDS if HOSTED else 300}


@app.get("/api/workspace")
async def workspace(request: Request):
    runs = [run for run in reversed(RUNS.values())
            if not HOSTED or RUN_OWNERS.get(run.id) == request.state.visitor]
    return {"active_run_id": next((run.id for run in runs if run.result is None), None),
            "latest_run_id": runs[0].id if runs else None,
            "runs": [summarize_run(run) for run in runs]}


@app.get("/api/capability")
async def get_capability(request: Request):
    capability, source = load_capability(request)
    return {"capability": capability.model_dump() if capability else None, "source": source}


def enforce_start_budget(visitor: str):
    now = time.monotonic()
    for key, timestamps in list(START_TIMES.items()):
        while timestamps and timestamps[0] <= now - RATE_WINDOW_SECONDS:
            timestamps.popleft()
        if not timestamps:
            del START_TIMES[key]
    while GLOBAL_START_TIMES and GLOBAL_START_TIMES[0] <= now - RATE_WINDOW_SECONDS:
        GLOBAL_START_TIMES.popleft()
    own_times = START_TIMES.get(visitor, ())
    if len(own_times) >= VISITOR_START_LIMIT or len(GLOBAL_START_TIMES) >= GLOBAL_START_LIMIT:
        raise HTTPException(429, "Demo run limit reached. Please try again in 10 minutes.", headers={"Retry-After": str(RATE_WINDOW_SECONDS)})


def discard_oldest_completed():
    if len(RUNS) < 30:
        return
    for run_id, run in list(RUNS.items()):
        task = TASKS.get(run_id)
        if run.result is not None and (task is None or task.done()):
            del RUNS[run_id]
            TASKS.pop(run_id, None)
            RUN_OWNERS.pop(run_id, None)
            if HOSTED:
                # Only this process's generated runtime directory is eligible.
                expected = ROOT / "runtime" / run_id
                if run.evidence_dir == expected:
                    shutil.rmtree(expected, ignore_errors=True)
            return


@app.post("/api/runs", status_code=202)
async def start(body: StartRequest, request: Request):
    visitor = request.state.visitor
    active = [run for run in RUNS.values() if run.result is None]
    if any(not HOSTED or RUN_OWNERS.get(run.id) == visitor for run in active):
        raise HTTPException(409, "Finish or abort the active run first")
    if HOSTED:
        if body.goal != FIXED_GOAL or set(body.inputs) != {"member_id"} or body.inputs["member_id"] not in {"1001", "1002"}:
            raise HTTPException(422, "The public demo accepts only its fixed savings workflow and synthetic members 1001 or 1002.")
        if body.mode == "discover" and not DISCOVERY_ENABLED:
            raise HTTPException(409, "Live discovery is available in the local project. This hosted demo replays the verified workflow without a model.")
        # A terminal result is published before Surface.close() completes. Keep
        # its slot reserved until the execution task has actually released the
        # browser; otherwise quick restarts can exceed the container budget.
        occupied = sum(run.result is None or (run.id in TASKS and not TASKS[run.id].done())
                       for run in RUNS.values())
        if occupied >= MAX_HOSTED_CONCURRENT:
            raise HTTPException(429, "The demo is busy. Please try again shortly.", headers={"Retry-After": "30"})
        enforce_start_budget(visitor)
    capability = None
    if body.mode == "replay":
        capability, _ = load_capability(request)
        if capability is None:
            raise HTTPException(409, "Discover a successful flow before replaying")
    options = {"deadline_seconds": HOSTED_RUN_SECONDS, "operator_timeout": HOSTED_OPERATOR_SECONDS} if HOSTED else {}
    run = Run(mode=body.mode, goal=body.goal, inputs=body.inputs,
              target_url=f"http://127.0.0.1:4311/?scenario={body.scenario}",
              capability=capability, evidence_root=ROOT / "runtime", **options)
    discard_oldest_completed()
    RUNS[run.id] = run
    if HOSTED:
        RUN_OWNERS[run.id] = visitor
        now = time.monotonic()
        START_TIMES.setdefault(visitor, deque()).append(now)
        GLOBAL_START_TIMES.append(now)
    TASKS[run.id] = asyncio.create_task(execute(run))
    return {"id": run.id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    return find_run(run_id, request).public_state()


@app.post("/api/runs/{run_id}/claim")
async def claim(run_id: str, request: Request):
    try:
        await find_run(run_id, request).claim()
    except ValueError:
        raise HTTPException(409, "Run is not awaiting operator control") from None
    return find_run(run_id, request).public_state()


@app.post("/api/runs/{run_id}/operator")
async def operator(run_id: str, body: OperatorRequest, request: Request):
    try:
        await find_run(run_id, request).operator_action(body.action)
    except ValueError:
        raise HTTPException(409, "Claim operator control first") from None
    except SurfaceError as exc:
        raise HTTPException(409, exc.code) from None
    return find_run(run_id, request).public_state()


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str, request: Request):
    try:
        await find_run(run_id, request).resume()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return find_run(run_id, request).public_state()


@app.post("/api/runs/{run_id}/abort")
async def abort(run_id: str, request: Request):
    run = find_run(run_id, request)
    if run.result is None:
        await run.abort()
    return run.public_state()
