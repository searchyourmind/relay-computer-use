# syntax=docker/dockerfile:1
# Build this directory as its own Railway service, without a public domain.
ARG OLLAMA_VERSION=0.34.0
FROM ollama/ollama:${OLLAMA_VERSION} AS official
# Ollama 0.34.0 installs CPU binaries/libraries at the top level and all GPU
# backends in subdirectories. Remove those before copying to the final image.
RUN find /usr/lib/ollama -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} + \
    && test -x /usr/lib/ollama/llama-server

FROM python:3.12-slim-trixie AS cpu
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OLLAMA_HOST=[::]:11434 \
    OLLAMA_MODELS=/opt/relay-model/models \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_NOHISTORY=1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_MAX_QUEUE=2 \
    OLLAMA_CONTEXT_LENGTH=2048 \
    OLLAMA_KEEP_ALIVE=60s \
    OLLAMA_LOAD_TIMEOUT=90s \
    LLAMA_ARG_CACHE_RAM=0 \
    CUDA_VISIBLE_DEVICES=-1 \
    ROCR_VISIBLE_DEVICES=-1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    PORT=11434
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libstdc++6 libgomp1 libopenblas0-pthread \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 relaymodel \
    && mkdir -p /opt/relay-model/models \
    && chown -R relaymodel:relaymodel /opt/relay-model
COPY --from=official /usr/bin/ollama /usr/bin/ollama
COPY --from=official /usr/lib/ollama /usr/lib/ollama
WORKDIR /opt/relay-model
COPY --chown=relaymodel:relaymodel launch.py bake.py ./
USER relaymodel

FROM cpu AS baked
ARG SOURCE_MODEL=qwen3:0.6b
# Download and verify at build time. Startup never pulls a model or contacts a
# model registry. Failure to fetch/configure the real model fails the build.
RUN python bake.py "${SOURCE_MODEL}"

FROM cpu AS runtime
COPY --from=baked --chown=relaymodel:relaymodel /opt/relay-model/models ./models
COPY --from=baked --chown=relaymodel:relaymodel /opt/relay-model/model.json ./model.json
EXPOSE 11434
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "launch.py", "--health"]
CMD ["python", "launch.py"]
