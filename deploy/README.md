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
