"""
Rate-limited, retrying, disk-caching client for the Dealroom API.

Rules from CLAUDE.md:
- Dealroom rate limit is 5 requests/second -> throttle, retry on 429 using Retry-After.
- Save every API response to /cache as JSON so repeat queries are instant.
"""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

MIN_INTERVAL = 1.0 / 5.0  # 5 req/sec
_last_request_time = [0.0]
_throttle_lock = threading.Lock()
_token_lock = threading.Lock()

_TOKEN_CACHE_PATH = config.CACHE_DIR / "_token_cache.json"


def _throttle():
    """Thread-safe: callers may run concurrently (see backtest.py/momentum.py,
    which fetch multiple companies' data in parallel to stay under the
    20-second budget), so the sleep-then-stamp has to be atomic across
    threads or two callers could both see 'clear to go' at once."""
    with _throttle_lock:
        now = time.time()
        elapsed = now - _last_request_time[0]
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request_time[0] = time.time()


def get_token(force_refresh: bool = False) -> str:
    if not force_refresh and _TOKEN_CACHE_PATH.exists():
        cached = json.loads(_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

    with _token_lock:  # avoid a thundering herd of concurrent token refreshes
        if not force_refresh and _TOKEN_CACHE_PATH.exists():
            cached = json.loads(_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"]
        return _refresh_token()


def _refresh_token() -> str:
    payload = json.dumps({
        "client_id": config.DEALROOM_CLIENT_ID,
        "client_secret": config.DEALROOM_CLIENT_SECRET,
        "audience": config.DEALROOM_AUDIENCE,
        "grant_type": "client_credentials",
    }).encode("utf-8")

    req = urllib.request.Request(
        config.DEALROOM_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    expires_in = data.get("expires_in", 86400)
    _TOKEN_CACHE_PATH.write_text(json.dumps({
        "access_token": data["access_token"],
        "expires_at": time.time() + expires_in,
    }), encoding="utf-8")
    return data["access_token"]


def _cache_path_for(path: str, params: dict) -> Path:
    key_material = json.dumps({"path": path, "params": params}, sort_keys=True)
    digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
    safe_name = path.strip("/").replace("/", "_")
    return config.CACHE_DIR / f"{safe_name}__{digest}.json"


def api_get(path: str, params: dict | None = None, use_cache: bool = True, max_retries: int = 5) -> dict:
    """GET against the Dealroom API. Returns the parsed JSON body.
    Raises RuntimeError on non-200 responses (after retries for 429).
    Caches successful responses to /cache; repeat identical calls are instant.
    """
    params = params or {}
    cache_file = _cache_path_for(path, params)

    if use_cache and cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    token = get_token()
    url = f"{config.DEALROOM_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="(),:|")

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Id": config.DEALROOM_CLIENT_ID,
        "User-Agent": "hindsight-hackathon-app/1.0",
    }

    last_error = None
    for attempt in range(max_retries):
        _throttle()
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if use_cache:
                    cache_file.write_text(json.dumps(body), encoding="utf-8")
                return body
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                retry_after = e.headers.get("Retry-After", "1")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1.0
                time.sleep(wait)
                last_error = f"429 rate limited (retried {attempt + 1}x)"
                continue
            raise RuntimeError(f"Dealroom API {e.code} for {path}: {body_text}")
    raise RuntimeError(f"Exceeded retries for {path}: {last_error}")
