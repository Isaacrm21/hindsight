"""
Loads settings from the project's env file.

Note: the credentials file in this project is named
dealroom-isaac-rodrigues-mendes.env, not literally ".env" — see SETUP.txt.
We load it explicitly by name so it works regardless of what the file is called.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

ENV_FILE_CANDIDATES = [
    PROJECT_ROOT / "dealroom-isaac-rodrigues-mendes.env",
    PROJECT_ROOT / ".env",
]


def _load_env_file() -> dict:
    for path in ENV_FILE_CANDIDATES:
        if path.exists():
            env = {}
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
            return env
    return {}


_env = _load_env_file()

DEALROOM_CLIENT_ID = _env.get("DEALROOM_CLIENT_ID") or os.environ.get("DEALROOM_CLIENT_ID")
DEALROOM_CLIENT_SECRET = _env.get("DEALROOM_CLIENT_SECRET") or os.environ.get("DEALROOM_CLIENT_SECRET")
ANTHROPIC_API_KEY = _env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

DEALROOM_BASE_URL = "https://api.beta.dealroom.app"
DEALROOM_TOKEN_URL = "https://accounts.dealroom.co/oauth/token"
DEALROOM_AUDIENCE = "https://api.beta.dealroom.app"

if not DEALROOM_CLIENT_ID or not DEALROOM_CLIENT_SECRET:
    raise RuntimeError(
        "Missing DEALROOM_CLIENT_ID / DEALROOM_CLIENT_SECRET. "
        f"Checked: {[str(p) for p in ENV_FILE_CANDIDATES]}"
    )
