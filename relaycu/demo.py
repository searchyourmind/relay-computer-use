"""Synthetic, HTML-only legacy banking surface for the browser demonstration.

Run with ``uvicorn relaycu.demo:app --host 127.0.0.1 --port 4311 --no-access-log``.
All names and balances are invented. No real accounts or banking service connects
to this application. Browser actions use ordinary form submissions and page links.
"""

from __future__ import annotations

import asyncio
import html
import secrets
from dataclasses import dataclass

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse


app = FastAPI(title="Relay Credit Union · Synthetic Workspace", docs_url=None, redoc_url=None, openapi_url=None)

SCENARIOS = {"normal", "not_found", "permission", "session", "transient", "error", "dialog", "slow", "ambiguous"}
COOKIE = "relay_demo_session"
MEMBERS = {
    "1001": {"name": "Avery Example", "balance": "4,250.75", "since": "2021"},
    "1002": {"name": "Morgan Sample", "balance": "9,180.20", "since": "2023"},
}


@dataclass
class Session:
    scenario: str = "normal"
    member: str | None = None
    fault_seen: bool = False
    pending: str | None = None


SESSIONS: dict[str, Session] = {}

CSS = """
:root{color-scheme:light;--navy:#122b3b;--ink:#183347;--muted:#667c87;--line:#dce6e8;--teal:#087d76;--pale:#edf5f4}
*{box-sizing:border-box}body{margin:0;background:#f4f7f8;color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}button,.button{border:0;border-radius:5px;background:var(--teal);color:white;padding:11px 18px;cursor:pointer;font:600 14px/1.2 inherit;display:inline-block;text-decoration:none}
button:hover,.button:hover{background:#096b65;text-decoration:none}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #d0a747;outline-offset:3px}
input{display:block;width:100%;max-width:320px;padding:11px 12px;border:1px solid #b8cbd0;border-radius:4px;margin:7px 0 17px;font:16px inherit;background:white;color:var(--ink)}
label{font-weight:600}h1,h2,p{margin-top:0}h1{font-size:29px;line-height:1.2;letter-spacing:-.6px;margin-bottom:12px}h2{font-size:20px;letter-spacing:-.2px;margin-bottom:10px}.eyebrow{font-size:11px;letter-spacing:1.7px;font-weight:700;text-transform:uppercase;color:var(--teal);margin-bottom:8px}
.muted{color:var(--muted)}.small{font-size:12px}.shell{max-width:1200px;margin:auto;padding:27px 34px}.top{background:var(--navy);color:#fff;padding:19px 34px}.top-inner{max-width:1132px;margin:auto;display:flex;justify-content:space-between;align-items:center}.brand{font-size:20px;font-weight:700;letter-spacing:-.4px}.brand-mark{display:inline-block;margin-right:10px;border:1px solid #5ba7a1;border-radius:7px;padding:2px 9px;color:#77d5c6}.tag{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;border:1px solid #48606c;border-radius:3px;padding:4px 9px;color:#a6c5cf}
.intro{display:flex;justify-content:space-between;align-items:end;margin-bottom:18px}.intro h1{font-size:26px;margin-bottom:3px}.surface{background:white;border:1px solid var(--line);border-radius:8px;overflow:hidden;box-shadow:0 10px 35px #17374809}iframe{display:block;width:100%;height:620px;border:0;background:white}.foot{display:flex;justify-content:space-between;gap:18px;color:var(--muted);font-size:12px;margin-top:15px}
.workspace{padding:0;background:white}.workspace-header{padding:16px 27px;background:#f7fafb;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}.layout{border-collapse:collapse;width:100%;min-height:540px}.rail{width:180px;vertical-align:top;padding:27px 19px;border-right:1px solid var(--line);background:#fbfcfd}.rail p{font-size:11px;font-weight:700;letter-spacing:1px}.rail span,.rail a{display:block;margin:15px 0;font-size:13px}.rail .active{color:var(--teal);font-weight:700}.main{padding:33px 32px;vertical-align:top}.panel{border:1px solid var(--line);border-radius:6px;padding:22px;margin-top:22px}.form-layout{width:100%;border-collapse:collapse}.form-layout td{vertical-align:top;padding:0}.hint{padding-left:32px!important;width:40%;color:var(--muted);font-size:13px}.hint strong{display:block;color:var(--ink);margin-bottom:7px}.data{width:100%;border-collapse:collapse;margin:17px 0 4px}.data th{text-align:left;color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:10px 10px;border-bottom:1px solid var(--line);background:#f7fafb}.data td{padding:16px 10px;border-bottom:1px solid var(--line)}.pill{background:#e8f3ef;color:#367661;border:1px solid #d1e7dd;border-radius:30px;padding:3px 9px;font-size:11px}.notice{border-left:3px solid #d0a747;background:#faf7ed;padding:18px 20px;margin:18px 0}.notice p{margin:4px 0 16px}.balance{background:var(--pale);border:1px solid #d3e6e1;border-radius:7px;padding:28px;margin-top:22px}.balance output{display:block;color:var(--navy);font-size:43px;font-weight:600;letter-spacing:-1.8px;margin:3px 0}.details{display:flex;gap:50px;margin-top:22px}.details strong{display:block;font-size:14px}.details span{font-size:12px;color:var(--muted)}dialog{max-width:480px;border:1px solid var(--line);padding:30px;border-radius:9px;box-shadow:0 15px 55px #122b3b33}dialog::backdrop{background:#122b3b66}.link-button{background:transparent;color:var(--teal);border:1px solid #afcfcb;margin-left:8px}.link-button:hover{background:#edf5f4;color:var(--teal)}
@media(max-width:750px){.shell{padding:20px 12px}.top{padding:15px}.tag,.rail,.hint{display:none}.main{padding:25px 18px}.intro{display:block}.foot{display:block}iframe{height:680px}.data td,.data th{padding:10px 6px}.balance output{font-size:36px}}
"""


def document(title: str, body: str) -> str:
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>'


def workspace(content: str, member: str | None = None) -> HTMLResponse:
    member_text = f"Member {html.escape(member)}" if member else "No member selected"
    body = f"""<div class="workspace"><header class="workspace-header"><span>Member services / Teller workspace</span><span>{member_text}</span></header>
    <table class="layout" role="presentation"><tr><td class="rail"><p>WORKSPACE</p><span class="active">Member services</span><span class="muted">Account servicing</span><span class="muted">Branch reports</span><hr style="border:0;border-top:1px solid var(--line);margin:27px 0"><a href="/workspace">New member search</a><span class="small muted">Training environment<br>Invented records only</span></td><td class="main"><main>{content}</main></td></tr></table></div>"""
    response = HTMLResponse(document("Member workspace · Relay", body))
    response.headers["Cache-Control"] = "no-store"
    return response


def state(request: Request) -> Session | None:
    return SESSIONS.get(request.cookies.get(COOKIE, ""))


def lost_session() -> HTMLResponse:
    return workspace('<h1>Session expired</h1><p>This training workspace needs to be opened again.</p><a class="button" href="/" target="_top">Open workspace</a>')


async def delay(session: Session) -> None:
    if session.scenario == "slow":
        await asyncio.sleep(0.8)


@app.get("/", response_class=HTMLResponse)
async def index(scenario: str = "normal"):
    if scenario not in SCENARIOS:
        return HTMLResponse(document("Unknown scenario", "<h1>Unknown scenario</h1><p>Choose a documented training scenario.</p>"), status_code=400)
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = Session(scenario=scenario)
    body = """<header class="top"><div class="top-inner"><div class="brand"><span class="brand-mark">R</span>Relay Credit Union</div><span class="tag">Synthetic training environment</span></div></header>
    <div class="shell"><section class="intro"><div><p class="eyebrow">Core banking · Branch operations</p><h1>Member services</h1><p class="muted" style="margin:0">A familiar workspace. A more reliable way to navigate it.</p></div><span class="small muted">Teller terminal / DEMO</span></section>
    <section class="surface"><iframe title="Member workspace" src="/workspace"></iframe></section><footer class="foot"><span>Demonstration only · All records are fictional · No real banking connection</span><span>Legacy workspace v1.0</span></footer></div>"""
    response = HTMLResponse(document("Relay Credit Union · Demo", body))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=3600)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/workspace", response_class=HTMLResponse)
async def search(request: Request):
    session = state(request)
    if session is None:
        return lost_session()
    session.member = None
    await delay(session)
    return workspace('''<p class="eyebrow">Member directory</p><h1>Find a member</h1><p class="muted">Search the member register to open an account overview.</p>
    <section class="panel"><table class="form-layout" role="presentation"><tr><td><form method="post" action="/workspace/find"><label for="member-id">Member ID</label><input id="member-id" name="member_id" inputmode="numeric" autocomplete="off" maxlength="24" required><button type="submit">Find member</button></form></td><td class="hint"><strong>Training records</strong>Try member <b>1001</b> (Avery Example) or <b>1002</b> (Morgan Sample). These are public, invented examples.</td></tr></table></section>''')


@app.post("/workspace/find", response_class=HTMLResponse)
async def find_member(request: Request, member_id: str = Form(...)):
    session = state(request)
    if session is None:
        return lost_session()
    await delay(session)
    member_id = member_id.strip()
    if session.scenario == "not_found" or member_id not in MEMBERS:
        session.member = None
        return workspace('<h1>Member search</h1><div class="notice" role="status"><h2>Member not found</h2><p>No matching record exists in this training register.</p></div><a class="button" href="/workspace">Search again</a>')
    session.member = member_id
    member = MEMBERS[member_id]
    extra = '<a class="button link-button" href="/workspace/profile">Open profile</a>' if session.scenario == "ambiguous" else ""
    return workspace(f'''<p class="eyebrow">Member directory</p><h1>Search results</h1><p class="muted">One matching member was found.</p><table class="data"><thead><tr><th>Member ID</th><th>Name</th><th>Membership</th><th>Action</th></tr></thead><tbody><tr><td>{member_id}</td><td><strong>{member['name']}</strong></td><td><span class="pill">Active</span></td><td><a class="button" href="/workspace/profile">Open profile</a>{extra}</td></tr></tbody></table>''', member_id)


def fault(session: Session, destination: str) -> HTMLResponse | None:
    if session.scenario == "permission":
        return workspace('<h1>Permission denied</h1><div class="notice" role="status"><p>This training role is not permitted to open the member profile.</p></div>', session.member)
    if session.scenario == "error":
        return workspace('<h1>Application error</h1><div class="notice" role="status"><p>The training application could not complete this request.</p></div>', session.member)
    if session.scenario == "dialog":
        return workspace('''<h1>Member profile</h1><dialog aria-labelledby="confirmation-title"><h2 id="confirmation-title">Unexpected confirmation</h2><p>An additional manual confirmation is required before continuing.</p><form method="dialog"><button>Cancel</button></form></dialog><script>document.querySelector('dialog').showModal()</script>''', session.member)
    if session.scenario in {"session", "transient"} and (not session.fault_seen or session.pending):
        session.fault_seen = True
        session.pending = destination
        title, action = ("Session expired", "Restore session") if session.scenario == "session" else ("Service temporarily unavailable", "Retry")
        return workspace(f'<h1>{title}</h1><div class="notice" role="status"><p>Your place in the workspace has been saved.</p><form method="post" action="/workspace/resume"><button type="submit">{action}</button></form></div>', session.member)
    return None


@app.post("/workspace/resume")
async def resume(request: Request):
    session = state(request)
    if session is None:
        return lost_session()
    destination = session.pending or "/workspace"
    session.pending = None
    return RedirectResponse(destination, status_code=303)


@app.get("/workspace/profile", response_class=HTMLResponse)
async def profile(request: Request):
    session = state(request)
    if session is None:
        return lost_session()
    if session.member not in MEMBERS:
        return RedirectResponse("/workspace", status_code=303)
    await delay(session)
    if blocked := fault(session, "/workspace/profile"):
        return blocked
    member = MEMBERS[session.member]
    return workspace(f'''<p class="eyebrow">Member record / {session.member}</p><h1>Member overview</h1><p class="muted">Review the member record and associated accounts.</p><section class="panel"><h2>{member['name']}</h2><span class="pill">Active membership</span><div class="details"><div><span>Member ID</span><strong>{session.member}</strong></div><div><span>Member since</span><strong>{member['since']}</strong></div><div><span>Record type</span><strong>Synthetic example</strong></div></div></section><p style="margin-top:24px"><a class="button" href="/workspace/accounts">View accounts</a></p>''', session.member)


@app.get("/workspace/accounts", response_class=HTMLResponse)
async def accounts(request: Request):
    session = state(request)
    if session is None:
        return lost_session()
    if session.member not in MEMBERS:
        return RedirectResponse("/workspace", status_code=303)
    await delay(session)
    member = MEMBERS[session.member]
    return workspace(f'''<p class="eyebrow">{member['name']} / Accounts</p><h1>Member accounts</h1><p class="muted">Select an account to view the current balance.</p><table class="data"><thead><tr><th>Account</th><th>Type</th><th>Status</th><th>Action</th></tr></thead><tbody><tr><td>Primary savings</td><td>Personal savings</td><td><span class="pill">Open</span></td><td><a class="button" href="/workspace/savings">Open savings</a></td></tr></tbody></table><p style="margin-top:22px"><a href="/workspace/profile">Back to member overview</a></p>''', session.member)


@app.get("/workspace/savings", response_class=HTMLResponse)
async def savings(request: Request):
    session = state(request)
    if session is None:
        return lost_session()
    if session.member not in MEMBERS:
        return RedirectResponse("/workspace", status_code=303)
    await delay(session)
    member = MEMBERS[session.member]
    return workspace(f'''<p class="eyebrow">{member['name']} / Primary savings</p><h1>Savings account</h1><p class="muted">Personal savings · <span class="pill">Open</span></p><section class="balance"><span class="small muted">CURRENT BALANCE · USD</span><output aria-label="Current balance">{member['balance']}</output><span class="small muted">Fictional training balance</span></section><div class="details"><div><span>Member ID</span><strong>{session.member}</strong></div><div><span>Product</span><strong>Primary savings</strong></div><div><span>Currency</span><strong>US dollar</strong></div></div><p style="margin-top:28px"><a href="/workspace/accounts">Back to accounts</a></p>''', session.member)
