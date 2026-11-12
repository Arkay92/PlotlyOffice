import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def release_id() -> str:
    env_val = os.environ.get("APP_RELEASE", "").strip()
    if env_val:
        return env_val
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("utf-8").strip() or "dev"
    except Exception:
        return "dev"
