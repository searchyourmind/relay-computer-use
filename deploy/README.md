# Hosted synthetic demo

Two services run in the same Railway project/environment:

1. The root Docker image serves Relay and its synthetic bank. Only the console
   has a public HTTPS domain; the bank binds to loopback port 4311.
2. `deploy/model/` is a standalone build context for a private CPU Ollama service.
   It bakes official `qwen3:0.6b` weights and exposes the configured alias
   `relay-discovery:latest` on private port 11434. Do not create a public domain
   or TCP proxy for this service.

Both services target the existing 1 GB memory limit. The application permits
one browser run at a time. The model permits one inference at a time and unloads
idle weights after 60 seconds. Model weights are verified and baked at build time;
startup needs no model download. See [model image details](model/README.md).

## Railway configuration

Build `deploy/model/` as its own root directory with its included Dockerfile and
Railway configuration. Name the service `relay-model`. Build the console from
the repository root. Configure the console:

```text
RELAY_HOSTED=1
RELAY_ENABLE_DISCOVERY=1
RELAY_PUBLIC_HOST=<exact generated HTTPS domain, without scheme or path>
OLLAMA_HOST=http://relay-model.railway.internal:11434
RELAY_MODEL=relay-discovery:latest
RELAY_MODEL_CONTEXT=2048
RELAY_MODEL_THINKING=1
PORT=8080
```

Keep both the console request and model image at a 2048-token context under the
current 1 GB limit. Request options override the model's baked defaults; the
4096-token cloud configuration exceeded this limit during inference. The deployed 2048-token configuration passed complete discovery
and replay. Sampled model memory was about 903 MB during the successful check
under a reported 1024 MB limit; monitoring samples are not a guarantee of every
instantaneous peak. The private model image also fixes `LLAMA_ARG_CACHE_RAM=0`:
completed-prompt snapshots would otherwise grow beyond the memory limit even
with a smaller active context. The local planner default remains 4096 tokens.
The default root image disables discovery until it is
explicitly enabled with a configured model endpoint. Replay needs no model.
The current deployment uses one replica and worker for each service. Temporary
visitor cookies, run ownership and capabilities are process-local and reset on
console redeployment. The checked-in capability remains available to new visitors.

The console supervisor stops its children if either the console or synthetic
bank exits. The model supervisor verifies the baked manifest and starts Ollama.
The model and console expose health checks through their Railway configs. A
successful health check is followed by actual discovery and replay validation.

## Optional Lovable frontend and server proxy

The Lovable application hosts its frontend and a TanStack Start
`createServerFn` proxy on Lovable. This integration does not use a separate
Supabase Edge Function. Python, Chromium, the synthetic bank and the private
Ollama service remain on Railway. Browser requests reach the same-origin Lovable
server function, which signs and forwards only supported Relay API requests to
the console's public HTTPS domain. No browser-to-Railway CORS exception is needed.

The published server runs in a different environment from Lovable's Node-based
editor preview. Read the secret from the server environment in either runtime.
Use `redirect: "manual"` and reject every 3xx response without following its
Location; the deployed runtime rejects `redirect: "error"`. Send
`Cache-Control: no-store` on upstream requests and server-function responses
instead of relying on the unsupported fetch cache option. Treat timeouts and
connection errors separately. A successful editor preview alone does not
validate the public deployment.

Set the same cryptographically random `RELAY_BRIDGE_SECRET` in the **server-only**
environment of the Lovable proxy and the Railway console. The value must contain
at least 32 UTF-8 bytes. Never put it in Git, prompts, client-exposed environment
variables, browser bundles, browser storage or logs. The proxy uses the exact
UTF-8 secret value as its HMAC key. The bridge is disabled when the console has
no sufficiently long secret; the original first-party console still works.

The browser creates an independent random 256-bit visitor capability token,
encoded as 43 URL-safe base64 characters. The proxy validates its format and
hashes it with SHA-256 before forwarding the visitor identifier. Treat that
browser token as a bearer capability: do not include it in URLs or logs. It is
not the shared bridge secret and is not an account login.

For each forwarded request, generate a new random 16-byte nonce and send:

| Header | Value |
| --- | --- |
| `X-Relay-Visitor` | SHA-256 of the browser token, 64 lowercase hex characters |
| `X-Relay-Timestamp` | Current Unix time in integer seconds |
| `X-Relay-Nonce` | Fresh nonce, 32 lowercase hex characters |
| `X-Relay-Signature` | HMAC-SHA256 of the message below, 64 lowercase hex characters |

The signed UTF-8 message has exactly six fields separated by newline characters,
with no trailing newline:

```text
timestamp + "\n" + nonce + "\n" + visitor + "\n" +
METHOD + "\n" + path + "\n" + sha256(rawBody)
```

`METHOD` is uppercase. `path` is the exact API path without a query string.
The final digest is lowercase hex over the exact bytes sent to Railway; a GET
with no body hashes the empty byte sequence. Serialize JSON once, then hash and
send those same bytes. Bodies are limited to 16 KiB, including chunked requests.

The bridge accepts only these method/path combinations:

| Method | Path |
| --- | --- |
| GET | `/api/config`, `/api/workspace`, `/api/capability` |
| GET | `/api/runs/{run_id}` |
| POST | `/api/runs` |
| POST | `/api/runs/{run_id}/claim`, `/api/runs/{run_id}/operator` |
| POST | `/api/runs/{run_id}/resume`, `/api/runs/{run_id}/abort` |

Every `run_id` must be 32 lowercase hex characters. Query strings, encoded paths,
other routes, duplicate or incomplete bridge headers and invalid signatures are
rejected. The exact configured Railway Host check remains mandatory. A failed
bridge request returns 403 and never falls back to a cookie identity or creates
a new cookie session.

Signatures allow at most 30 seconds of clock difference. Freshness is checked
again after reading the body, so a slow body cannot revive an expired request.
Each authenticated nonce is single-use and retained for 61 seconds, covering
the entire clock-skew window. The process retains at most 4096 live nonces and
fails closed if this bound is reached; expired entries are reclaimed on valid
requests. Generate a fresh nonce and signature for every request, including
polls and retries. This capacity supports ordinary 1.5-second polling with ample
room for this single-browser synthetic demo.

Bridge visitors use a separate internal namespace from Railway cookie visitors.
Opening both frontends therefore does **not** share a workspace, run history or
visitor-discovered capability. Run reads and operator controls remain restricted
to their owning visitor. All state and replay protection remain process-local:
keep one console replica and one web worker. The existing limits are unchanged:
one browser run globally, six starts per visitor per ten minutes, thirty starts
globally per ten minutes, a 180-second run deadline and a 90-second operator
timeout. Only the fixed savings workflow and synthetic members 1001 and 1002
are accepted. The bridge does not add arbitrary browsing or remove these limits.

## Local container check

```sh
docker build -t relay-demo .
docker run --rm --name relay-demo --memory=1g -p 4310:8080 \
  -e RELAY_HOSTED=0 relay-demo
```

Select Replay for a model-free container check. For local discovery use the root
README, or configure a reachable private Ollama endpoint. All records are synthetic.

References: [Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles),
[Railway healthchecks](https://docs.railway.com/deployments/healthchecks),
[private networking](https://docs.railway.com/networking/private-networking).
