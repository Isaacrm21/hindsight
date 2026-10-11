"""
Hindsight — FastAPI backend.

Endpoints:
  POST /api/resolve-thesis   free text -> best-guess location + tag IDs (for confirmation in the UI)
  GET  /api/resolve-fund     fund name -> investor ID candidates (for confirmation in the UI)
  POST /api/analyze          2019-2020 seed cohort -> scoring, fund performance card, "ones that got away"
  POST /api/today            same thesis, last 18 months, ranked by momentum
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import dealroom_client as dc
from . import scoring
from . import thesis_resolver
from .config import PROJECT_ROOT

app = FastAPI(title="Hindsight")


class ThesisRequest(BaseModel):
    thesis: str


class AnalyzeRequest(BaseModel):
    location_id: int
    tag_ids: list[int]
    fund_name: str | None = None
    year_from: int = 2019
    year_to: int = 2020


class TodayRequest(BaseModel):
    location_id: int
    tag_ids: list[int]
    months_back: int = 18


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/resolve-thesis")
def resolve_thesis(req: ThesisRequest):
    try:
        return thesis_resolver.resolve_thesis(req.thesis)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/resolve-fund")
def resolve_fund(name: str):
    try:
        body = dc.api_get("/data/investors", {"filter": f"name[eq]:{name}", "limit": 5})
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    candidates = [
        {"uuid": inv.get("uuid"), "name": inv.get("name"), "domain": inv.get("domain")}
        for inv in body.get("data", [])
    ]
    return {"query": name, "candidates": candidates}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    fund_uuid = None
    if req.fund_name:
        try:
            body = dc.api_get("/data/investors", {"filter": f"name[eq]:{req.fund_name}", "limit": 1})
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if body.get("data"):
            fund_uuid = body["data"][0]["uuid"]
        else:
            raise HTTPException(status_code=404, detail=f"No investor found matching '{req.fund_name}'")

    try:
        result = scoring.analyze_thesis(
            req.location_id, req.tag_ids, fund_uuid, req.year_from, req.year_to
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@app.post("/api/today")
def today(req: TodayRequest):
    try:
        return scoring.analyze_today(req.location_id, req.tag_ids, req.months_back)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/")
def index():
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")
