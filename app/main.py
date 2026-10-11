"""
Hindsight — basic app skeleton.

Just the plumbing for now: a FastAPI backend with a working Dealroom
connection (rate-limited, retrying, cached to /cache) and a blank page.
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import backtest as bt
from . import curated_options
from . import dealroom_client as dc
from . import llm_thesis
from . import momentum
from .config import PROJECT_ROOT

app = FastAPI(title="Hindsight")


class BacktestRequest(BaseModel):
    location_id: int
    tag_ids: list[int]
    round_type: str = "Seed"
    year_from: int
    year_to: int
    fund_name: str | None = None


class ParseThesisRequest(BaseModel):
    thesis: str


class MomentumRequest(BaseModel):
    location_id: int
    tag_ids: list[int]
    months_back: int = 18


@app.get("/api/health")
def health():
    """Proves the FastAPI server itself is up (no Dealroom call)."""
    return {"ok": True}


@app.get("/api/dealroom-check")
def dealroom_check():
    """Proves the Dealroom connection, rate limiting, and caching all work —
    fetches the companies filter registry (a small, harmless call) and reports
    whether this response came from /cache or a live request."""
    cache_file = dc._cache_path_for("/reference/filters", {"scope": "companies"})
    was_cached = cache_file.exists()
    try:
        body = dc.api_get("/reference/filters", {"scope": "companies"})
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "connected": True,
        "served_from_cache": was_cached,
        "filter_count": len(body.get("data", [])),
    }


@app.post("/api/parse-thesis")
def parse_thesis(req: ParseThesisRequest):
    """Plain-English thesis -> structured backtest input, via the Claude API,
    grounded against real Dealroom filter values. Always returns 200 — a
    parse failure is reported as success=false with fallback dropdown
    options, never an error. The demo must never break because of this."""
    parsed = llm_thesis.parse_thesis_with_llm(req.thesis)
    if parsed:
        return {"success": True, **parsed}
    return {
        "success": False,
        "reason": (
            "Couldn't confidently match that to real Dealroom filters "
            "(or the Claude API key is missing/unavailable) — pick manually below."
        ),
        "fallback_options": {
            "locations": curated_options.CURATED_LOCATIONS,
            "industries": curated_options.CURATED_INDUSTRIES,
        },
    }


@app.post("/api/backtest")
def backtest(req: BacktestRequest):
    """The backtest feature: thesis in, fund-performance results out."""
    try:
        return bt.run_backtest(
            req.location_id, req.tag_ids, req.round_type,
            req.year_from, req.year_to, req.fund_name,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/momentum")
def momentum_endpoint(req: MomentumRequest):
    """The momentum feature: same thesis, last N months, ranked by how fast
    each company is moving right now."""
    try:
        return momentum.rank_by_momentum(req.location_id, req.tag_ids, req.months_back)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# Always re-fetch the page and the demo data: a browser holding a stale copy
# of either shows an old UI against new data (seen live more than once).
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(PROJECT_ROOT / "static" / "index.html", headers=NO_CACHE)


@app.get("/demo-data.js")
def demo_data():
    """Pre-captured, verified results for ?demo=1 mode — see static/demo-data.js."""
    return FileResponse(PROJECT_ROOT / "static" / "demo-data.js", media_type="application/javascript",
                        headers=NO_CACHE)
