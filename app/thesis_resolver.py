"""
Turns a free-text investment thesis ("UK seed-stage fintech") into Dealroom
filter IDs (a location + one or more taxonomy tags).

This is necessarily a best-effort guess, not a sure thing — Dealroom's search
endpoint does fuzzy text matching per phrase, it doesn't split a whole thesis
into "location part" + "industry part" for you (tested live; see NOTES.md).

Design choice: resolve_thesis() returns its best guess PLUS every candidate
it considered, so the frontend can show the guess as editable chips and the
user can correct it before any scoring runs. Per the project's own skill
file (dealroom-api-analysis/SKILL.md): wrong-tag-family mistakes return a
plausible-looking but wrong number with no error — so silent trust is the
one thing we must not do here.
"""
import re

from . import dealroom_client as dc

STOPWORDS = {
    "seed", "seed-stage", "stage", "round", "rounds", "companies", "company",
    "startups", "startup", "backed", "focused", "focus", "in", "the", "a",
    "an", "of", "and", "or", "for", "based", "stage-agnostic",
}

LOCATION_ALIASES = {
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "us": "United States",
    "u.s.": "United States",
    "usa": "United States",
    "eu": "European Union",
    "europe": "Europe",
}

# Prefer these location kinds over a city/state when we have a choice.
BROAD_LOCATION_TYPES = {"continent", "region", "country"}

# Within the broad types, an explicit priority — not just entities_count.
# Per SKILL.md's "three Europes": Dealroom's search can return a 0
# entities_count for a bare query like "Europe" (tested live), which makes
# ranking by count alone unstable/order-dependent. SKILL.md is explicit:
# default to the REGION (includes Turkey, excludes Israel) when someone just
# says "Europe" — a single country is unambiguous either way, so this only
# ever breaks a continent-vs-region-vs-"incl X" tie.
LOCATION_TYPE_PRIORITY = {"country": 0, "region": 1, "continent": 2}

# Family priority per SKILL.md: prefer a typed tag over the grey "sector" catch-all.
TAXONOMY_FAMILY_PRIORITY = {
    "industry": 0,
    "technology": 1,
    "sub_industry": 2,
    "sdg": 3,
    "business_model": 4,
    "income_stream": 5,
    "client_focus": 6,
    "ownership": 7,
    "sector": 8,  # last resort
}


def _candidates_from_text(text: str) -> list[str]:
    cleaned = text.strip()
    candidates = [cleaned]

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]*", text.lower())
    significant = [w for w in words if w not in STOPWORDS]

    # aliases first (uk -> United Kingdom etc.)
    for w in significant:
        if w in LOCATION_ALIASES:
            candidates.append(LOCATION_ALIASES[w])

    # bigrams, then single words, longest phrases first
    for n in (3, 2, 1):
        for i in range(len(significant) - n + 1):
            phrase = " ".join(significant[i:i + n])
            candidates.append(phrase)

    # dedupe, keep order
    seen = set()
    out = []
    for c in candidates:
        key = c.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _search(query: str) -> list[dict]:
    body = dc.api_get("/reference/filters/search", {
        "scope": "transactions", "q": query, "limit": 15, "limit_per_row": 5,
    })
    return body.get("data", [])


def rank_location_candidates(location_rows: list[dict]) -> list[dict]:
    def location_sort_key(row):
        source_type = row.get("source_type")
        broad = source_type in BROAD_LOCATION_TYPES
        type_rank = LOCATION_TYPE_PRIORITY.get(source_type, 9)
        return (not broad, type_rank, -(row.get("entities_count") or 0))
    return sorted(location_rows, key=location_sort_key)


def rank_taxonomy_candidates(taxonomy_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (best_tags, all_deduped_sorted)."""
    seen_ids = set()
    dedup_taxonomy = []
    for row in taxonomy_rows:
        if row.get("value") and row["value"] not in seen_ids:
            seen_ids.add(row["value"])
            dedup_taxonomy.append(row)

    def taxonomy_sort_key(row):
        family = row.get("pinned_sub_type") or row.get("source_type") or "sector"
        pri = TAXONOMY_FAMILY_PRIORITY.get(family, 9)
        return (pri, -(row.get("entities_count") or 0))

    dedup_taxonomy_sorted = sorted(dedup_taxonomy, key=taxonomy_sort_key)

    # Per SKILL.md: a typed tag (industry/technology/...) beats the grey "sector"
    # catch-all, and a sector decoy shouldn't dilute a good typed match. So: take
    # the best tag, then only add further tags from that SAME family (e.g. a
    # second "technology" tag for "AI and machine learning"). Only fall back to
    # sector-family tags if nothing typed matched at all.
    best_tags = []
    if dedup_taxonomy_sorted:
        top = dedup_taxonomy_sorted[0]
        top_family = top.get("pinned_sub_type") or top.get("source_type") or "sector"
        best_tags = [top] + [
            row for row in dedup_taxonomy_sorted[1:]
            if (row.get("pinned_sub_type") or row.get("source_type") or "sector") == top_family
        ][:2]

    return best_tags, dedup_taxonomy_sorted


def resolve_thesis(thesis_text: str) -> dict:
    candidates = _candidates_from_text(thesis_text)

    location_rows: list[dict] = []
    taxonomy_rows: list[dict] = []

    for cand in candidates[:8]:  # cap queries for speed
        rows = _search(cand)
        for row in rows:
            if row.get("kind") != "value":
                continue
            if row.get("filter_key") == "hq_location":
                location_rows.append(row)
            elif row.get("filter_key") == "taxonomy_id":
                taxonomy_rows.append(row)
        # stop early once we have decent signal, to keep this fast
        if location_rows and taxonomy_rows:
            break

    location_rows_sorted = rank_location_candidates(location_rows)
    best_location = location_rows_sorted[0] if location_rows_sorted else None

    best_tags, dedup_taxonomy_sorted = rank_taxonomy_candidates(taxonomy_rows)

    warnings = []
    if not best_location:
        warnings.append("Could not resolve a location from this thesis — pick one manually.")
    if not best_tags:
        warnings.append("Could not resolve an industry/technology tag — pick one manually.")
    if best_tags and any(
        (t.get("pinned_sub_type") or t.get("source_type")) == "sector" for t in best_tags
    ) and not any(
        (t.get("pinned_sub_type") or t.get("source_type")) in ("industry", "technology") for t in best_tags
    ):
        warnings.append(
            "Only a broad 'sector' tag matched (Dealroom's catch-all grey tag family) — "
            "double-check this is really the population you mean."
        )

    return {
        "thesis_text": thesis_text,
        "best_location": best_location,
        "location_candidates": location_rows_sorted[:8],
        "best_tags": best_tags,
        "tag_candidates": dedup_taxonomy_sorted[:10],
        "warnings": warnings,
    }
