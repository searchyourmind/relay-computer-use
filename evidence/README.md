# Captured execution evidence

The original local captures below are actual executions against the synthetic banking UI on
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

Each local directory includes structured events and sanitized state observations.
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

## Hosted discovery

The public Railway deployment was exercised on September 22, 2026 with genuine
private CPU inference: Qwen3 0.6B, alias `relay-discovery:latest`, 2048-token
context, thinking enabled, and completed-prompt snapshot caching disabled.
Configured model digest:
`6fba63b47371d91de091a39084b3b2f7f3741c108de1c52be2f92688afad24c5`.

- `cloud-discover/`: run `bd7128844f764ba2a04c4900dbbf26fd` completed five real
  model decisions and five browser actions in 28.04 seconds.
- `cloud-replay/`: run `2a216c759aa349c4b8d84a6438c1bbda` reused that same newly
  learned capability for another synthetic member in 8.06 seconds, with zero model calls.
- The same visitor's capability endpoint identified the artifact as newly
  discovered, and the replay capability's SHA-256 matched it. Both returned the
  expected, different synthetic balances; persisted outputs remain redacted.

These cloud captures contain sanitized API events/results and the learned
capability, rather than the local filesystem's per-step snapshots. The model
service is private; no scripted model-response fallback is used. Native HTML
validity excludes form submissions the browser would currently block, without
reading input values into evidence or prescribing a navigation plan.

## Signed frontend bridge

[`lovable-bridge/validation.json`](lovable-bridge/validation.json) records actual
cloud API checks on September 22 through the new server-to-server signing path.
Discovery completed five model calls; replay used that same capability with a
different synthetic member and zero model calls. A session-expiry run was
claimed, restored and resumed in the same browser session. A second visitor
could neither read nor control the first visitor's runs. These are backend
integration checks; they do not stand in for frontend UI verification.

## Automated verification

The complete test suite passed **194 tests** on Python 3.14 / macOS ARM64.
It includes provider response validation, control-order invariance, real candidate binding, hosted visitor isolation and limits, independent page routes, redacted workspace history, corrupted capability handling, cancellation during model/browser awaits, richer snapshot privacy, real Chromium flows, missing/ambiguous controls, cross-origin and
multi-hop redirects, popup/WebSocket restrictions, unexpected dialogs,
same-session recovery, input and schema validation, forged completion,
bounded retries/timeouts, and sensitive sentinel non-persistence. The 47 bridge
checks also cover signed body/path/method binding, visitor isolation, chunked
bodies, nonce replay and requests that expire while their body is being read. Two upstream
test-client deprecation warnings remain; there were no test failures.

Unit-test planner doubles are confined to tests and are not the discovery
evidence. Replay tests fail if a planner is constructed or called.

## Published Lovable UI

`lovable-bridge/public-ui.json` records checks performed through the published
Lovable frontend. Real discovery completed five actions with five model calls;
replay for a different synthetic member completed with zero calls after a
same-session operator claim, page navigation, reload, restore and resume. A
missing-member run correctly stopped after two actions with `business_outcome`,
without marking the remaining steps complete. Event filtering, unknown-run
recovery and mobile layout were also checked. Balances and visitor tokens are
not included in this observation record.

## Reproduce

Use the commands in the root README. A live Ollama model is required only for
discovery. The checked-in artifact supports all replay and operator examples
without a running model service. Published evidence excludes raw provider
transcripts, input values, balances, browser cookies, screenshots and full HTML.
