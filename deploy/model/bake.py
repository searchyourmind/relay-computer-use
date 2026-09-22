"""Bake one genuine Ollama model and its constrained CPU defaults into an image."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
MODEL = "relay-discovery:latest"
BASE_URL = "http://127.0.0.1:11434"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PARAMETERS = {"num_ctx": 2048, "num_thread": 2, "num_gpu": 0, "num_batch": 128}


def request(path: str, data=None, *, timeout=30):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(BASE_URL + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def tag_digest(name: str) -> str:
    rows = request("/api/tags")["models"]
    return next(row["digest"] for row in rows if row["name"] == name)


def verify_blobs():
    models = Path(os.environ["OLLAMA_MODELS"])
    for manifest in (models / "manifests").rglob("*"):
        if not manifest.is_file():
            continue
        content = json.loads(manifest.read_text())
        for layer in [content["config"], *content["layers"]]:
            algorithm, digest = layer["digest"].split(":", 1)
            if algorithm != "sha256":
                raise RuntimeError("Unexpected model blob digest algorithm")
            blob = models / "blobs" / f"sha256-{digest}"
            with blob.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest or blob.stat().st_size != layer["size"]:
                raise RuntimeError("Baked model blob verification failed")


def main():
    source = sys.argv[1]
    if not source or source == MODEL or ":cloud" in source or any(c.isspace() for c in source):
        raise ValueError("An explicit local source model tag is required")
    env = os.environ.copy()
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    server = subprocess.Popen(["ollama", "serve"], env=env)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("Ollama stopped while preparing the image")
            try:
                version = request("/api/version", timeout=2)["version"]
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
        else:
            raise RuntimeError("Ollama did not become ready during the build")
        subprocess.run(["ollama", "pull", source], env=env, check=True, timeout=1200)
        source_digest = tag_digest(source)
        request("/api/create", {"model": MODEL, "from": source,
                                "parameters": PARAMETERS, "stream": False}, timeout=180)
        model_digest = tag_digest(MODEL)
        verify_blobs()
        metadata = {"source_model": source, "source_digest": source_digest,
                    "model": MODEL, "digest": model_digest,
                    "ollama_version": version, "parameters": PARAMETERS}
        (ROOT / "model.json").write_text(json.dumps(metadata, indent=2) + "\n")
        print(json.dumps(metadata), flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
