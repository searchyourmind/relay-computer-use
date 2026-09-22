# Private discovery model

This directory is a separate Docker build context. It runs real Ollama inference
for Relay's discovery planner, without a public domain, a paid model API, or a
model download during container startup. Deploy it in the same Railway project
and environment as the public console. Do not enable public HTTP or TCP access:
Ollama's native API has no authentication in this private service.

The Docker build copies Ollama 0.34.0 CPU binaries from the official image into a
slim Debian runtime. GPU backend directories are removed before the final copy.
The `SOURCE_MODEL` build argument selects the downloaded model; the app always
requests `relay-discovery:latest`. That alias adds `num_ctx=2048`, `num_thread=2`,
`num_gpu=0`, and `num_batch=128`. The build verifies every model blob against its manifest digest
and records source/configured digests in `/opt/relay-model/model.json`.

```sh
docker build --build-arg SOURCE_MODEL=qwen3:0.6b -t relay-model deploy/model
docker run --rm -p 127.0.0.1:11434:11434 --memory=1g relay-model
```

On Railway, create a private service from this directory and keep `PORT=11434`.
Point the console's model URL at `http://<service>.railway.internal:11434` and set
its model name to `relay-discovery:latest`. Set the console's
`RELAY_MODEL_CONTEXT=2048` too: request options override the baked model defaults.
Keep this hosted context setting at 2048 under the current 1 GB service limit;
4096 exceeded that limit during cloud inference. The local planner's 4096-token
default is separate from this hosted configuration.
The service listens on `[::]:11434`
to accept Railway private IPv6 traffic and dual-stack local probes. Railway's
`/api/tags` health check confirms the server has started; a complete discovery
run is still required to validate inference and memory use before enabling it
for visitors. Docker's health check also verifies the baked alias and digest.

Only one inference runs at a time and only one model stays loaded. Requests can
queue up to two deep; the model unloads after 60 seconds idle. The separate
llama-server prompt RAM cache is disabled with `LLAMA_ARG_CACHE_RAM=0`, preventing
completed prompts from accumulating extra KV snapshots inside a 1 GB service.
This leaves the active inference KV buffer intact. Ollama inherits this variable
into its runner; no model or image replacement is needed to set it on an existing
service and restart. Startup checks the
model manifest and fails if it is missing or changed. A model/backend error is a
real discovery failure: there is no scripted decision fallback.

The image's actual final size and a 1 GB constrained inference run must be checked
on the target architecture. Model quantization size alone does not establish
runtime memory fit. The source tag can be changed after evaluating model quality;
the build does not claim a small model passes Relay's workflow until tested.

References: [Ollama 0.34.0 image layout](https://github.com/ollama/ollama/blob/v0.34.0/Dockerfile),
[CPU/GPU install layout](https://github.com/ollama/ollama/blob/v0.34.0/llama/server/CMakeLists.txt),
[Ollama configuration](https://github.com/ollama/ollama/blob/v0.34.0/envconfig/config.go),
[runner environment inheritance](https://github.com/ollama/ollama/blob/v0.34.0/llm/llama_server.go),
[prompt cache configuration in pinned llama.cpp b10760](https://github.com/ggml-org/llama.cpp/blob/b10760/common/arg.cpp),
[Railway private networking](https://docs.railway.com/networking/private-networking).
