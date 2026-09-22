"""Start the baked private CPU model; never download or replace it on startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def metadata():
    data = json.loads((ROOT / "model.json").read_text())
    if data["model"] != "relay-discovery:latest":
        raise RuntimeError("Unexpected baked model")
    manifest = Path(os.environ["OLLAMA_MODELS"]) / "manifests/registry.ollama.ai/library/relay-discovery/latest"
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != data["digest"]:
        raise RuntimeError("Baked model manifest is missing or changed")
    return data


def main():
    data = metadata()
    if sys.argv[1:] == ["--health"]:
        with OPENER.open("http://[::1]:11434/api/tags", timeout=3) as response:
            rows = json.load(response)["models"]
        if not any(row["name"] == data["model"] and row["digest"] == data["digest"] for row in rows):
            raise RuntimeError("The baked model is not available")
        return
    if sys.argv[1:]:
        raise ValueError("Unknown launcher argument")
    # These are fixed service limits, not user-supplied discovery parameters.
    env = os.environ.copy()
    env.update({"OLLAMA_HOST": "[::]:11434", "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_NOHISTORY": "1", "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_MAX_QUEUE": "2",
                "OLLAMA_CONTEXT_LENGTH": "2048", "OLLAMA_KEEP_ALIVE": "60s",
                "LLAMA_ARG_CACHE_RAM": "0",
                "CUDA_VISIBLE_DEVICES": "-1", "ROCR_VISIBLE_DEVICES": "-1",
                "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"})
    print(f"relay-model: serving {data['model']} from {data['source_model']} ({data['digest']})", flush=True)
    # Ollama receives platform termination signals directly as PID 1.
    os.execve("/usr/bin/ollama", ["ollama", "serve"], env)


if __name__ == "__main__":
    main()
