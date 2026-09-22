# Relay

**Discover a UI workflow with a model. Save the contract. Replay it without one.**

Relay implements one complete capability: look up a synthetic member and return
their savings balance through an intentionally old-fashioned banking interface.
The target uses an iframe, nested tables, links, and HTML forms. The executor
never calls a target application's business API or reads its internal state.

The demonstration contains invented records only. It connects to no real bank
and requires no credentials. The project supports real model discovery, saved
capabilities, and model-free replay.

**[Open the Lovable frontend](https://relay-computer-use.lovable.app/configure)**
· [Original Railway console](https://relay-production-ff74.up.railway.app/configure)

The new frontend uses React and TanStack Start on Lovable. Its same-origin
server function signs a restricted set of API requests to the Railway backend.
Python, Playwright/Chromium, the synthetic bank and private Ollama inference
remain in the two Railway Docker services. This demo has no database: visitor
workspaces and run ownership live in the console process's memory and reset on
restart. The two frontends give visitors separate workspaces.

This repository contains the Python backend, original console and deployment
configuration. The new frontend and its server proxy are maintained in the
[Lovable project](https://lovable.dev/projects/1c026fe0-2bd2-45da-b329-833186b60965),
not in this repository. See the [bridge deployment notes](deploy/README.md#optional-lovable-frontend-and-server-proxy)
for the integration contract.

Try **Discover** with member `1001`, then **Replay** with member `1002`.
The **Session handoff** and **Missing member** presets exercise recovery and business outcomes. Each visitor
gets a separate temporary workspace and browser session. The hosted service uses
synthetic members only, allows one active run globally, and bounds each run to
three minutes (90 seconds for operator intervention). Runs expire on restart;
this is a review sandbox, not a production banking service.

Hosted discovery is configured for Qwen3 0.6B with thinking enabled and a
2048-token context in a separate private CPU model service. The deployed 1 GB services have completed real discovery and replay;
[cloud execution evidence](evidence/README.md#hosted-discovery) records the result. The local default remains Qwen3.5 2B with a 4096-token
context. No paid model API or user-supplied key is needed.
[Deployment notes](deploy/README.md) document the two services and hosted boundary.

## Review in three minutes

1. Inspect [the saved capability](evidence/capability.json): parameter references,
   exact targets, typed output and a success checkpoint form the callable contract.
2. Compare [real discovery events](evidence/discovery/events.jsonl) with
   [the independent replay result](evidence/replay/result.json): five model decisions
   discovered the flow; replay invoked it with another member and **zero** model calls.
3. Follow [the handoff events](evidence/handoff/events.jsonl): automation pauses,
   an operator claims and restores the same session, then returns control.
   [Evidence notes](evidence/README.md) explain provenance and redaction.
4. Read [the design report](REPORT.md) for the policy boundary, error taxonomy,
   trade-offs and deliberate cuts. To try it, follow the model-free replay below.

## Setup

Python 3.11+ and a recent Chromium-compatible operating system are required.
The verified development environment uses Python 3.14 on macOS ARM64.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m playwright install chromium
```

For real discovery, install [Ollama](https://ollama.com/download), start it, and run:

```sh
ollama pull qwen3.5:2b
```

For the smaller hosted model configuration locally:

```sh
ollama pull qwen3:0.6b
export RELAY_MODEL=qwen3:0.6b
export RELAY_MODEL_CONTEXT=2048
export RELAY_MODEL_THINKING=1
```

The default model endpoint is `http://127.0.0.1:11434`. `OLLAMA_HOST` and
`RELAY_MODEL` can override it. No paid model API or API key is required. Only
allowlisted UI labels, the goal, input **names**, and prior action descriptors
are sent to the model; member IDs, names, and balances are not.

Start the target application in one terminal:

```sh
.venv/bin/uvicorn relaycu.demo:app --host 127.0.0.1 --port 4311 --no-access-log
```

## Discover, then replay

With the target and Ollama running:

```sh
.venv/bin/python -m relaycu.cli discover \
  --goal "Find the current savings balance for the supplied member." \
  --member-id 1001 --save runtime/my-capability.json

.venv/bin/python -m relaycu.cli replay \
  --artifact runtime/my-capability.json --member-id 1002
```

Discovery uses actual model decisions to fill and navigate the live browser.
The observation excludes native form submissions that browser validation would
currently block, so a required field must be valid before its submit action
becomes a candidate. This reads DOM validity, not input values or a fixed plan.
Each request presents a canonical list of legal actions from the current UI.
The model chooses a descriptive candidate; the planner maps it to the current
control ID. This keeps input bindings and control/action pairs valid and makes
equivalent observations independent of control enumeration order.
The saved artifact contains parameter references, typed outputs, exact semantic
targets, and an independently verified success checkpoint. It contains neither
the example member ID nor a model transcript.

Replay uses the recorded actions and a new member parameter. It returns an exact
decimal string for the balance; the model call count must be **0**. A replay
cannot invoke an LLM recovery path.

### Run without a model

Ollama can be stopped for the following commands. The checked-in capability was
generated by the recorded discovery run in `evidence/`.

```sh
.venv/bin/python -m relaycu.cli replay --member-id 1002
.venv/bin/python -m relaycu.cli replay --member-id 9999
.venv/bin/python -m relaycu.cli replay --member-id 1002 \
  --target 'http://127.0.0.1:4311/?scenario=transient'
```

The second command returns `business_outcome / not_found`, not a crash. The
third performs a bounded, explicitly allowlisted retry and verifies the output.

## Operator console and live handoff

In another terminal:

```sh
.venv/bin/uvicorn relaycu.server:app --host 127.0.0.1 --port 4310 --no-access-log
```

Open [the console](http://127.0.0.1:4310). Choose Replay, member `1002`, and the
session-expiry scenario. The run pauses with a sanitized view of the **same**
browser session. Click **Claim session**, **Restore session**, then **Return
control**. The operator command clicks the recovery control in the paused
browser, and replay continues from its saved position. The event log records
ownership transitions and operator actions against one session ID.

The console has four directly addressable pages:

| Page | Purpose |
| --- | --- |
| [Configure](http://127.0.0.1:4310/configure) | Choose discovery or replay, invocation parameters and a demo scenario. |
| [Execution](http://127.0.0.1:4310/execution) | Follow the active run, inspect its result and handle operator intervention. |
| [Activity](http://127.0.0.1:4310/activity) | Inspect the current run's ordered decisions, actions and control transfers. |
| [Workflows](http://127.0.0.1:4310/workflows) | Inspect and download the reusable capability contract. |

The console is deliberately a minimal operator surface, not full co-browsing.
It exposes only trusted recovery actions. Unknown dialogs, ambiguous targets,
permission failures and app errors stop for intervention; the operator may
abort. The console cannot override policy or approve an irreversible action.

For the same interaction in a terminal:

```sh
.venv/bin/python -m relaycu.cli replay --member-id 1002 --operator \
  --target 'http://127.0.0.1:4311/?scenario=session'
```

Enter `restore` when prompted. `--headed` exposes the isolated application
browser for inspection. Use the operator console or CLI for control transfer;
untracked direct browser manipulation is not an audited recovery path.

## Results and evidence

The result contract distinguishes `success`, `business_outcome`, and `failure`.
Outputs go to the caller; persisted results record output field names with
redacted values. `runtime/<run-id>/` contains sanitized state snapshots,
structured events, and the result. Successful discovery also saves a capability.
Failure snapshots contain UI structure and allowlisted labels, not raw HTML or
screenshots with customer data. Runtime files are ignored by Git.
The structural inventory separates frames and records matched headings and
controls, including their visibility and enabled state. Only policy-defined
names are eligible; input values and unknown application text are excluded.

See [evidence/README.md](evidence/README.md) for the captured real discovery,
independent replay, and exceptional-state demonstrations. See
[REPORT.md](REPORT.md) for decisions, boundaries and deliberate cuts.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Tests use invented records and isolated browsers; model decisions in unit tests
are doubles and do not substitute for the real discovery evidence. Browser
tests cover policy boundaries, ambiguity, runtime outcomes and session recovery.
The saved artifact is validated before replay, and input values cannot become
selectors or executable code.

## Scope

The demonstration's discovery contract is savings-balance lookup. Natural
language describes the goal within that contract; arbitrary banking tasks are
not supported. The executor's trusted policy permits only this product's routes
and a small vocabulary of read-only navigation and recovery controls. It is
configured separately from capabilities in `relaycu/surface.py` (`Policy`).
The general flow representation is reusable, but onboarding another product
requires a reviewed policy, observation vocabulary and output contract.

No native desktop adapter, real authentication, distributed workers, production
operator authentication, or institutional tenant deployment infrastructure is implemented.
Those boundaries are described in the report rather than claimed as features.
