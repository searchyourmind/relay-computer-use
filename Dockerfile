# syntax=docker/dockerfile:1
# The hosted review surface runs deterministic replay within a 1 GB plan.
# Live discovery connects to a separate private model service when enabled.
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    RELAY_HOSTED=1 \
    RELAY_ENABLE_DISCOVERY=0 \
    PORT=8080
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 relay
COPY requirements.lock ./
RUN python -m pip install --requirement requirements.lock \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/launch.py deploy/launch.py
RUN mkdir -p /app/runtime && chown relay:relay /app/runtime
USER relay
COPY --chown=relay:relay pyproject.toml README.md REPORT.md ./
COPY --chown=relay:relay relaycu ./relaycu
COPY --chown=relay:relay evidence ./evidence
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; host='healthcheck.railway.app' if os.getenv('RELAY_HOSTED')=='1' else '127.0.0.1:4310'; r=urllib.request.Request('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health',headers={'Host':host}); urllib.request.urlopen(r,timeout=4).read()"
CMD ["python", "deploy/launch.py"]
