"""Pre-download the Kokoro-82M snapshot into the HuggingFace cache.

The installer calls this so the first launch is quick and so a blocked network
fails here, with a readable message, instead of surfacing as a mysteriously slow
startup later. Safe to re-run: an already-cached snapshot is a no-op.

    venv\\Scripts\\python.exe tools\\fetch_model.py
"""

from __future__ import annotations

import os
import sys

# Without Developer Mode, Windows cannot make the symlinks the hub cache prefers,
# and it says so in a long warning that reads like a failure mid-install. The
# degraded path it falls back to is fine here -- one model, cached once.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

REPO_ID = "hexgrad/Kokoro-82M"


def main() -> int:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is not installed", file=sys.stderr)
        return 1

    try:
        path = snapshot_download(REPO_ID)
    except Exception as exc:  # network, proxy, auth -- all equally fatal here
        print(f"could not download {REPO_ID}: {exc}", file=sys.stderr)
        return 1

    print(f"model cached at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
