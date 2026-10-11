"""
Turns a plain-English thesis into the backtest's structured format, using
the Claude API — but grounded, not trusted blind.

Design: Claude never invents a Dealroom ID. It only extracts plain-language
intent (a location name, an industry name, a round type, years, a fund name)
from the free text. We then look those plain-language terms up against the
REAL Dealroom filter registry ourselves (the same tested resolver logic from
thesis_resolver.py) and only accept IDs that Dealroom's own search actually
returned. If Claude is unavailable, times out, returns something we can't
parse, or resolves to nothing real — we give up cleanly and return None.
The caller falls back to the dropdown menus. This function must never raise.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request

from . import config
from . import thesis_resolver

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
REQUEST_TIMEOUT_SECONDS = 12

VALID_ROUND_TYPES = {
    "Pre-Seed", "Seed", "Series A", "Series B", "Series C",
    "Series D", "Series E", "Growth",
}

EXTRACTION_PROMPT = """Extract a structured investment thesis from the text below.
Respond with ONLY a single JSON object — no other text, no markdown fences —
matching exactly this shape:

{{
  "location_query": "<a plain place name to search for, e.g. United Kingdom, Europe, United States>",
  "industry_query": "<a plain industry/technology name to search for, e.g. fintech, artificial intelligence>",
  "round_type": "<one of: Pre-Seed, Seed, Series A, Series B, Series C, Series D, Series E, Growth — default Seed if not stated>",
  "year_from": <integer, default 2019 if not stated>,
  "year_to": <integer, default 2020 if not stated>,
  "fund_name": "<a named VC fund to compare against, or null if none mentioned>"
}}

Text: "{thesis}"

Respond with only the JSON object."""


def _call_claude(thesis_text: str) -> dict | None:
    if not config.ANTHROPIC_API_KEY:
        return None

    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(thesis=thesis_text)}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    text = body["content"][0]["text"].strip()
    # strip markdown fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def parse_thesis_with_llm(thesis_text: str) -> dict | None:
    """Returns a validated, Dealroom-grounded thesis dict, or None on any failure."""
    try:
        extracted = _call_claude(thesis_text)
        if not extracted:
            return None

        location_query = extracted.get("location_query")
        industry_query = extracted.get("industry_query")
        if not location_query or not industry_query:
            return None

        # Ground both terms against the REAL Dealroom filter registry —
        # reusing the exact same tested search + ranking logic as the
        # free-text resolver (thesis_resolver.py), not a separate copy of
        # it. Only IDs Dealroom's own search returns are ever used.
        # The two searches are independent, so run them concurrently.
        with ThreadPoolExecutor(max_workers=2) as pool:
            location_future = pool.submit(thesis_resolver._search, location_query)
            tag_future = pool.submit(thesis_resolver._search, industry_query)
            location_rows = [
                r for r in location_future.result()
                if r.get("kind") == "value" and r.get("filter_key") == "hq_location"
            ]
            tag_rows = [
                r for r in tag_future.result()
                if r.get("kind") == "value" and r.get("filter_key") == "taxonomy_id"
            ]

        location_rows_sorted = thesis_resolver.rank_location_candidates(location_rows)
        best_location = location_rows_sorted[0] if location_rows_sorted else None

        best_tags, _ = thesis_resolver.rank_taxonomy_candidates(tag_rows)
        best_tag = best_tags[0] if best_tags else None

        if not best_location or not best_tag:
            return None  # nothing real to ground this in -> fall back

        round_type = extracted.get("round_type")
        if round_type not in VALID_ROUND_TYPES:
            round_type = "Seed"

        year_from = extracted.get("year_from")
        year_to = extracted.get("year_to")
        if not isinstance(year_from, int) or not isinstance(year_to, int):
            year_from, year_to = 2019, 2020
        if year_from > year_to:
            year_from, year_to = year_to, year_from

        fund_name = extracted.get("fund_name")
        if not isinstance(fund_name, str) or not fund_name.strip():
            fund_name = None

        return {
            "location_id": int(best_location["value"]),
            "location_label": best_location["label"],
            "tag_ids": [int(best_tag["value"])],
            "tag_labels": [best_tag["label"]],
            "round_type": round_type,
            "year_from": year_from,
            "year_to": year_to,
            "fund_name": fund_name,
            "source": "llm",
        }
    except Exception:
        # Anything at all — missing key, network error, timeout, bad JSON,
        # unexpected response shape, Dealroom hiccup — falls back silently.
        # The demo must never break because of this feature.
        return None
