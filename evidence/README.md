# Captured execution evidence

These are actual executions against the live local synthetic banking UI on
September 22, 2026, not scripted model responses. The discovery model was
`qwen3.5:2b`, digest
`324d162be6ca5629ae4517c8710434d0bd2d665bc94dbad46e9af8fbf8a2f0df`.

| Run | Evidence directory | Verified behavior |
| --- | --- | --- |
| Discovery | `discovery/` | Five live model decisions drove five browser actions; independent checkpoint/output validation passed. |
| Replay, different member | `replay/` | Same saved artifact, different invocation parameter, successful output, zero model calls. |
| Missing member | `not-found/` | Terminal `business_outcome / not_found`; no later profile/account action. |
| Session expiry | `handoff/` | Paused at step 3, operator claimed control, restored the same session, returned control; remaining steps succeeded without a model. |
| Cancel during model planning | `cancelled/` | A real model run was cancelled; no browser action follows the abort event, one terminal failure is recorded, and safe expected/observed diagnostics are returned. |
| Temporary failure | `transient/` | One explicitly permitted retry; successful output, zero model calls. |

`capability.json` is copied directly from the successful discovery artifact.
It records parameter references and typed output extraction, not the discovery
input. The normal discovery used invented member 1001; the independent replay
and recovery runs used invented member 1002. The test suite checks the different
expected outputs. Persisted results deliberately redact output values, including
these synthetic values, to demonstrate the same persistence boundary.

Each directory includes structured events and sanitized state observations.
`handoff/failure-step-3.json` adds a richer structural failure snapshot. The snapshot includes bounded per-frame inventories of trusted headings and controls, with visibility and enabled state. The same
session ID appears before, during, and after the ownership transitions; it is a
random correlation identifier, not a browser cookie or credential.

The latest discovery, replay, missing-member, handoff, and cancellation captures were repeated after the four-page console redesign. The handoff was also tested across navigation, browser Back, and refresh before resuming.

The handoff capture was exercised through the actual operator console by the
coding assistant acting as the operator. It demonstrates the real pause/claim/
action/resume mechanism; it does not claim that a separate human participant
performed the recording. The synthetic Restore session button substitutes for
production reauthentication. No page or session was replaced to fake recovery.

An earlier development attempt navigated successfully but then proposed an
invalid extra action at the terminal page. The final controller now recognizes
the declared checkpoint independently, and the captured discovery above uses
that behavior. This does not imply a perfect planner for arbitrary goals.

## Automated verification

The complete test suite passed **100 tests** on Python 3.14 / macOS ARM64.
It includes independent page routes, redacted workspace history, corrupted capability handling, cancellation during model/browser awaits, richer snapshot privacy, real Chromium flows, missing/ambiguous controls, cross-origin and
multi-hop redirects, popup/WebSocket restrictions, unexpected dialogs,
same-session recovery, input and schema validation, forged completion,
bounded retries/timeouts, and sensitive sentinel non-persistence. Two upstream
test-client deprecation warnings remain; there were no test failures.

Unit-test planner doubles are confined to tests and are not the discovery
evidence. Replay tests fail if a planner is constructed or called.

## Reproduce

Use the commands in the root README. A live Ollama model is required only for
discovery. The checked-in artifact supports all replay and operator examples
without a running model service. Published evidence excludes raw provider
transcripts, input values, balances, browser cookies, screenshots and full HTML.
