"""
"Who's moving right now": the same thesis, run on seed rounds from the last N
months (default 18), ranked by Dealroom's own signal_rating.

The rating is Dealroom's model of a startup's prospects, so the ranking is one
explainable number the audience already trusts rather than a home-made score.
Headcount and web traffic are shown beside it as context, not used to rank.
Traffic trends are computed from the monthly web-traffic series: the summary
`traffic.growth_3m` field was seen live contradicting that series (ProGrad
reported +516,616% while its monthly visits had halved).

Sanity check, unchanged: a company is excluded if it was founded before 2021,
or if a later-stage round predates the matched "seed" (so the "seed" is really
a bridge or extension of an older company).
"""
from concurrent.futures import ThreadPoolExecutor

from . import dealroom_client as dc
from . import scoring

COMPANY_BATCH_SIZE = 200  # matches the batch size already confirmed working in NOTES.md
SANITY_CHECK_WORKERS = 10
MIN_BASE_VISITS_FOR_SIGNAL = 50  # below this a percentage change is noise, not a trend
FOUNDING_YEAR_FLOOR = 2021
CANDIDATE_POOL_CAP = 20  # exclusion rate observed live is ~12%, so 20 comfortably yields a full top 10

# Rounds that mean a company was already past seed stage BEFORE the matched
# "seed" transaction — i.e. this "seed" is really a later/bridge round in
# disguise. A second Seed or Pre-Seed tranche does NOT go here: multi-close
# seed rounds are completely normal practice and shouldn't be penalized —
# only a genuine escalation to institutional-stage funding is disqualifying.
LATER_STAGE_ROUNDS = {
    "Series A", "Series B", "Series C", "Series D", "Series E", "Series F+",
    "Growth", "IPO", "Secondary", "Debt", "Bridge",
}


def _format_visits(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def _team_context(company: dict) -> str | None:
    size, growth = company.get("employees_count"), company.get("employee_count_1y_growth")
    if size is None and growth is None:
        return None
    team = f"team of {size:,}" if size is not None else "team size not on file"
    if growth is None:
        return team
    if abs(growth) < 0.5:
        return f"{team}, flat over 12 months"
    return f"{team}, {'up' if growth > 0 else 'down'} {abs(growth):.0f}% over 12 months"


def _traffic_context(uuid: str) -> str | None:
    series = dc.api_get(f"/data/companies/{uuid}/web-traffic", {}).get("data") or []
    if not series:
        return None
    last = series[-1]
    target = (last["year"] * 12 + last["month"]) - 3
    base = next((p for p in series if p["year"] * 12 + p["month"] == target), None)
    now = last.get("visits") or 0
    if base is None or (base.get("visits") or 0) < MIN_BASE_VISITS_FOR_SIGNAL:
        return f"{_format_visits(now)} visits a month"
    change = 100 * (now / base["visits"] - 1)
    if change >= 900:
        return f"{_format_visits(now)} visits a month, up from {_format_visits(base['visits'])} 3 months ago"
    return f"{_format_visits(now)} visits a month, {'up' if change >= 0 else 'down'} {abs(change):.0f}% over 3 months"


def _fetch_full_companies(uuids: list[str]) -> dict[str, dict]:
    """signal_rating and traffic are `view=full` fields on /data/companies, not on the
    transaction-embedded company object."""
    chunks = [uuids[i:i + COMPANY_BATCH_SIZE] for i in range(0, len(uuids), COMPANY_BATCH_SIZE)]
    if not chunks:
        return {}

    def fetch_chunk(chunk):
        filt = "or(" + ",".join(f"id[eq]:{u}" for u in chunk) + ")"
        return dc.api_get("/data/companies", {"filter": filt, "limit": len(chunk), "view": "full"})

    with ThreadPoolExecutor(max_workers=min(len(chunks), SANITY_CHECK_WORKERS)) as pool:
        bodies = list(pool.map(fetch_chunk, chunks))
    return {c["uuid"]: c for body in bodies for c in body.get("data", [])}


def _parse_year(launch_date: str | None) -> int | None:
    if not launch_date:
        return None
    try:
        return int(launch_date[:4])
    except (ValueError, TypeError):
        return None


def _sanity_check(name: str, location_id: int, tag_ids: list[int], matched_seed_date: str):
    """Fetches the company's full history and checks whether the matched
    'seed' round is genuinely early-stage: nothing at Series A or beyond
    (institutional-stage funding) should predate it. A second Seed/Pre-Seed
    tranche before it is fine — multi-close seed rounds are normal.
    Returns (ok: bool, reason_if_excluded: str|None, seed_amount)."""
    journey = scoring.fetch_company_journey(name, location_id, tag_ids)
    rounds = journey["rounds"]
    if not rounds:
        return False, "no round history found to verify against", None

    def ym(r):
        month = r.get("month") or 1
        return (r.get("year") or 0, month)

    matched_key = None
    matched = next((r for r in rounds if r["date"] == matched_seed_date), None)
    seed_amount = matched.get("amount") if matched else None
    if matched:
        matched_key = ym(matched)

    if matched_key is None:
        return False, "matched seed round not found in full history (date mismatch)", seed_amount

    earlier_later_stage = [
        r for r in rounds
        if ym(r) < matched_key and (r.get("standardized_round") in LATER_STAGE_ROUNDS)
    ]
    if earlier_later_stage:
        culprit = earlier_later_stage[0]
        return (False,
                f"a {culprit.get('standardized_round')} round ({culprit['date']}) predates the matched seed round "
                f"— this 'seed' is really a later/bridge round, not a fresh debut raise",
                seed_amount)

    return True, None, seed_amount


def rank_by_momentum(location_id: int, tag_ids: list[int], months_back: int = 18, top_n: int = 10):
    cohort = scoring.get_recent_seed_cohort(location_id, tag_ids, months_back=months_back)
    full = _fetch_full_companies(list(cohort["companies"].keys()))

    rated = []
    skipped_no_rating = 0
    for info in cohort["companies"].values():
        company = full.get(info["uuid"]) or info["company_obj"]
        rating = company.get("signal_rating")
        if rating is None:
            skipped_no_rating += 1
            continue
        rated.append({
            "uuid": info["uuid"],
            "name": info["name"],
            "domain": info["domain"],
            "seed_round_date": info["seed_round_date"],
            "founding_year": _parse_year(company.get("launch_date")),
            "signal_rating": rating,
            "team": _team_context(company),
        })
    rated.sort(key=lambda r: (-r["signal_rating"], r["name"] or ""))

    pool_candidates = rated[:CANDIDATE_POOL_CAP]
    with ThreadPoolExecutor(max_workers=SANITY_CHECK_WORKERS) as pool:
        check_results = list(pool.map(
            lambda c: _sanity_check(c["name"], location_id, tag_ids, c["seed_round_date"]),
            pool_candidates,
        ))

    verified, excluded = [], []
    for candidate, (ok, exclude_reason, seed_amount) in zip(pool_candidates, check_results):
        candidate["seed_round_amount"] = seed_amount
        if not ok:
            candidate["excluded_reason"] = exclude_reason
        elif candidate["founding_year"] is None:
            candidate["excluded_reason"] = "no founding year on file, so it could not be verified"
        elif candidate["founding_year"] < FOUNDING_YEAR_FLOOR:
            candidate["excluded_reason"] = f"founded {candidate['founding_year']}, before {FOUNDING_YEAR_FLOOR}"
        (excluded if "excluded_reason" in candidate else verified).append(candidate)

    top = verified[:top_n]
    with ThreadPoolExecutor(max_workers=SANITY_CHECK_WORKERS) as pool:
        for row, traffic in zip(top, pool.map(lambda r: _traffic_context(r["uuid"]), top)):
            row["traffic"] = traffic

    return {
        "location_id": location_id,
        "tag_ids": tag_ids,
        "months_back": months_back,
        "cutoff_date": cohort["cutoff_date"],
        "filter": cohort["filter"],
        "total_matches": cohort["total_matches"],
        "truncated": cohort["truncated"],
        "unique_companies": len(cohort["companies"]),
        "skipped_no_rating": skipped_no_rating,
        "candidates_checked": len(pool_candidates),
        "excluded_by_sanity_check": excluded,
        "top_10_by_signal": top,
    }
