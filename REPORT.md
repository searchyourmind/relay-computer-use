# Architecture

Relay separates the model, the surface, the reusable contract, and the run
controller. `OllamaPlanner` proposes one action from a sanitized observation.
`Surface` owns browser observation, targeting, execution and trusted policy.
`Run` owns completion, evidence, recovery and control transfer. A FastAPI console
and CLI call the same controller. The target is a separate synthetic application
with no business API exposed to the runner.

The chosen slice is savings-balance lookup: search, open a member profile, view
accounts, and open savings. This provides a multi-step read operation without
introducing a financial mutation. Discovery's input/output contract is supplied
by the application; the model discovers the steps, not the authorization or
success definition. Qwen3.5 2B through local Ollama keeps the example reproducible
without a paid provider. A larger model could improve planning, but the executor
must remain equally strict. An initial discovery attempt reached the right
screen but proposed another action. Completion now uses the declared checkpoint
and output validation independently of model self-reporting.

# Artifact schema

`Capability` is a strict Pydantic model. It declares a schema version, capability
name/revision, product/version, typed inputs and outputs, ordered actions, and a
terminal checkpoint. Unknown fields, undeclared input references and unsupported
schema versions are rejected. Fill actions contain `input_ref`, never a recorded
member ID. Output extraction returns an exact decimal string, avoiding binary
floating-point ambiguity for money.

Targets use exact accessible labels or roles and names, with a named frame
scope. Frame and element matches must be unique. There is no silent first-match
fallback, arbitrary JavaScript, or model-generated selector. This is more
reviewable than recorded coordinates or generated code, though it requires
enough accessibility information in the application. A capability cannot grant
itself routes, controls, network access or permission to commit a transaction.

# Determinism & error handling

Replay does not instantiate a planner. It validates parameters, executes the
ordered actions, inspects exceptional states between steps, and verifies the
terminal checkpoint and output types. It awaits the actual iframe navigation,
not only the outer page. Browser waits, overall run time, step count, retries and
operator waits are bounded. Cancellation interrupts an in-flight model or browser await, joins session cleanup, and records one terminal outcome. Failures return safe expected/observed diagnostics. Determinism means fixed decisions and targeting;
network timing and current business data can still change.

`not_found` and `validation` are expected business outcomes. A known temporary
service failure permits at most two `Retry` clicks; this recovery is safe for
the read-only lookup and is logged separately from the learned flow. Session
expiry pauses for an operator. Permission failures, unknown confirmations,
application errors, missing/ambiguous targets and policy violations stop rather
than guessing a next action. A missing checkpoint is failure even if the model
says it is done. Failure evidence identifies the step, expected state and safe
observed condition. No uncertain financial mutation is retried.

# Heterogeneity & multi-tenant

The implemented adapter drives a real browser through a titled iframe and
table-based forms without test IDs. Observation, resolution, action and extraction
are the extension seam; flow and ownership logic do not need to understand DOM
nodes. A desktop adapter would need its own accessibility/window scoping and
validated controls. Screenshot-only targets would require anchors and confidence
checks, not reuse of browser coordinates. Neither is claimed to be implemented.

For multiple institutions, I would separate a reviewed vendor capability and
compatible product versions from a tenant binding (entry point, branding and
semantic aliases). A tenant override could narrow an executor policy, never
expand it. Product/version checks, failed locators and checkpoints would mark a
binding incompatible and require a reviewed rediscovery. Capabilities should be
promoted through fixture replay and sampled tenant validation before deployment.
The current product/version fields describe compatibility; production version
detection and tenant binding storage are deliberately not built.

# Escalation & handoff

Ownership moves from automation to awaiting operator, then to human control and
back to automation. While awaiting or ceded, the engine waits on a continuation
event and performs no automated steps. Claim, operator action and resume are
serialized. Recovery operates on the existing `Surface` and page, preserving
the session and next-step pointer. Resume re-observes the UI and requires a ready
state; the next exact locator and final checkpoint still protect continuation.

The minimal console routes an intervention with a reason, step, sanitized live
state and session ID. Its Restore session control genuinely operates the paused
browser. It records the operator action and both ownership transitions. It does
not simulate recovery by starting another browser or marking the step successful.
The operator can abort, and unhandled conditions time out. Hard selector/policy
failures remain failures; operator takeover is not a bypass for those boundaries.

# Safety

Policy is trusted configuration outside the artifact. It allowlists exact
origins/routes, action types and semantic controls. Unknown actions and
irreversible controls are blocked even if a model calls them safe. The isolated
browser has no personal profile or real credentials. Network routing, frame
checks, popup/download/WebSocket restrictions and per-action checks constrain
the surface. Redirect destinations are checked before following them.

Persistence is opt-in by field, not an attempted regex scrub of an entire DOM.
Observations contain only a trusted label vocabulary and UI structure. Inputs
are represented by references; goals, parameter values, balances, cookies, raw
model output, HTML and screenshots are not written to evidence. Extracted data
returns only through the result boundary. Logs store decision codes and model
metadata, not free-form reasoning. A production target would still require a
security review of its allowed routes and controls: an allowed application can
itself send data or change behavior. This is not a general-purpose banking
security sandbox.

# Cuts

One process and one active console run keep ownership and debugging clear. I
did not build queues, a capability marketplace, cloud deployment, real bank
integration or full co-browsing. The demo session-recovery button stands in for
human reauthentication; its control transfer and browser continuity are real,
but it is not a production login implementation. Console authentication and
operator identity are local-development assumptions, not enterprise guarantees.

Next I would add authenticated operator leases, explicitly versioned tenant
bindings, retention controls, and a second substantially different target before
expanding the action vocabulary. I would also validate another model against
the same checkpoints and adversarial fixtures. These additions should preserve
the small reviewed execution surface and zero-model replay path.
