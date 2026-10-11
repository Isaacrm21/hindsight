"""
Core Hindsight pipeline: seed cohort -> later-stage matching -> scoring,
plus a momentum ranking for "today's matches".

Scoring ("Power Law-style", per CLAUDE.md — exit/revenue data isn't in the API):
  reached Series A       = 10 points
  reached Series B+      = 30 points  (highest round tier only, not cumulative with the above)
  latest valuation >= $1bn = 100 points
  exited                 = 20 points
"""
import datetime
from concurrent.futures import ThreadPoolExecutor

from . import dealroom_client as dc

MAX_PAGES = 6  # safety cap per query (6 x 500 = 3000 rows); truncation is reported, never hidden
PAGE_FETCH_WORKERS = 6  # remaining pages (after the first) fetched concurrently by offset


# Dealroom's own VC view: venture rounds only, Mature companies and Outside Tech excluded
VC_DEFAULT_LEGS = "is_vc_round[eq]:true,growth_stage[nin_any]:412,taxonomy_id[nin_any]:1102801"

STAGE_RANK = {"Pre-Seed": 0, "Seed": 1, "Series A": 2, "Series B": 3, "Series C": 4,
              "Series D": 5, "Growth": 5, "Series E": 6, "Series F+": 7, "series_f_": 7, "IPO": 8}


def _tag_filter_leg(tag_ids: list[int]) -> str:
    # One leg per tag: repeating the key means AND, while piping values into one leg would mean OR
    return ",".join(f"taxonomy_id[eq]:{t}" for t in tag_ids)


def with_legs(*legs: str) -> str:
    return "and(" + ",".join(leg for leg in legs if leg) + ")"


def is_after(row: dict, entry: dict) -> bool:
    # Rounds are dated to the month at best, so ties and missing months fall back to stage order
    if row.get("year") != entry.get("year"):
        return (row.get("year") or 0) > (entry.get("year") or 0)
    if row.get("month") and entry.get("month") and row["month"] != entry["month"]:
        return row["month"] > entry["month"]
    return STAGE_RANK.get(row.get("standardized_round"), -1) > STAGE_RANK.get(entry.get("stage"), -1)


# Real company names routinely contain characters the filter DSL itself
# uses as syntax (commas separate AND-terms, pipes mean OR-within-a-leg,
# parens group expressions) -- e.g. "Vaxxinity | Covaxx", "Family Proud,
# Inc", "Xogo (formerly Bansen Labs)". All confirmed live in one real
# cohort. There's no documented escaping for this filter grammar, so a name
# containing any of these characters can't safely go into a hand-built
# filter string at all -- not batched, not even alone. Building one anyway
# produces a 400 that would otherwise take the whole request down with it
# (one oddly-named company in a top 10 shouldn't be able to crash the demo).
UNSAFE_NAME_CHARS = set(",|()[]:")


def is_filter_safe_name(name: str) -> bool:
    return not any(ch in UNSAFE_NAME_CHARS for ch in name)


def fetch_company_journey(name: str, location_id: int, tag_ids: list[int]) -> dict:
    """Full round-by-round history for one company, via the company.name
    relationship filter (precise, one small request — see NOTES.md). Shared
    by backtest.py (top-10 journeys) and momentum.py (founding-year /
    true-seed sanity check)."""
    if not is_filter_safe_name(name):
        return {"rounds": [], "ambiguous": True, "name_unsafe": True}

    tag_leg = _tag_filter_leg(tag_ids)
    filt = f"and(company.name[eq]:{name},hq_location[eq]:{location_id},{tag_leg})"
    body = dc.api_get("/data/transactions", {"filter": filt, "limit": 50, "sort": "date"})
    rounds = []
    for t in body.get("data", []):
        rounds.append({
            "year": t.get("year"),
            "month": t.get("month"),
            "date": f"{t.get('year')}-{t.get('month') or '?'}",
            "round_type": t.get("round_type"),
            "standardized_round": t.get("standardized_round"),
            "amount": t.get("amount"),
            "valuation_at_round": t.get("valuation"),
        })
    return {"rounds": rounds, "ambiguous": len(body.get("data", [])) == 0, "name_unsafe": False}


def paginate(path: str, filt: str, limit: int = 500, sort: str | None = None, max_pages: int = MAX_PAGES):
    """Fetches up to MAX_PAGES pages (MAX_PAGES x limit rows), truncation
    reported never hidden. The first page is fetched alone (to learn the
    real total via include_total), then any remaining pages are fetched
    CONCURRENTLY by offset rather than one-at-a-time by cursor — offset
    pagination gives the same, non-overlapping, consistently-ordered rows
    (verified live), and for a large cohort (e.g. 2,469 rows = 5 pages) this
    is the difference between ~10s and ~2s. This is what makes the
    20-second budget achievable for a thesis with a big population."""
    base_params = {"filter": filt, "limit": limit, "include_total": "true"}
    if sort:
        base_params["sort"] = sort

    first = dc.api_get(path, {**base_params, "offset": 0})
    total = first["page"].get("total")
    rows = list(first["data"])

    if total is not None and total > len(rows):
        remaining_offsets = list(range(limit, min(total, limit * max_pages), limit))
        if remaining_offsets:
            with ThreadPoolExecutor(max_workers=PAGE_FETCH_WORKERS) as pool:
                pages = list(pool.map(
                    lambda off: dc.api_get(path, {**base_params, "offset": off}),
                    remaining_offsets,
                ))
            for page in pages:
                rows.extend(page["data"])

    truncated = total is not None and total > len(rows)
    return rows, total, truncated


def score_company(company: dict):
    """CLAUDE.md scoring is explicit: 'latest valuation of $1bn or more'.
    That means the CURRENT valuation figure only — not Dealroom's separate
    "unicorn" classification label, which can stay true even after a
    company's on-file valuation has since dropped (see The Bank of London
    in NOTES.md-style testing: certified a unicorn in 2021, but its current
    figure on file is $220m). Current valuation is the test, limited to companies
    founded in 1990 or later per Dealroom's valuation defaults."""
    classifications = company.get("classifications") or []
    labels = {c.get("name") if isinstance(c, dict) else c for c in classifications}
    is_exited = "exited" in labels
    valuation = company.get("valuation") or {}
    val_amount = valuation.get("value")
    launch = str(company.get("launch_date") or "")[:4]
    founded = int(launch) if launch.isdigit() else None
    high_valuation = val_amount is not None and val_amount >= 1_000_000_000 and founded is not None and founded >= 1990
    return is_exited, high_valuation, val_amount


def momentum_score(company: dict) -> float:
    """Simple composite: recent headcount growth % + traffic growth % (capped) + a hiring bonus."""
    emp_growth = company.get("employee_count_1y_growth") or 0
    traffic = company.get("traffic") or {}
    traffic_growth = traffic.get("growth_3m") or 0
    # a single viral traffic spike shouldn't dominate the ranking — cap it
    traffic_growth = max(-100, min(traffic_growth, 500))
    jobs = company.get("jobs") or {}
    hiring_bonus = 15 if jobs.get("is_hiring") else 0
    open_roles_bonus = min((jobs.get("open_count") or 0), 20)  # cap
    return round(emp_growth + (traffic_growth * 0.5) + hiring_bonus + open_roles_bonus, 2)


def get_seed_cohort(location_id: int, tag_ids: list[int], year_from: int, year_to: int):
    tag_leg = _tag_filter_leg(tag_ids)
    filt = (
        f"and(standardized_round[eq]:Seed,year[gte]:{year_from},year[lte]:{year_to},"
        f"hq_location[eq]:{location_id},{tag_leg})"
    )
    rows, total, truncated = paginate(filt=filt, path="/data/transactions", sort="-date")

    companies: dict[str, dict] = {}
    for t in rows:
        c = t["company"]
        u = c["uuid"]
        if u not in companies:
            investors = t.get("investors") or []
            companies[u] = {
                "uuid": u,
                "name": c["name"],
                "domain": c.get("domain"),
                "company_obj": c,
                "seed_round_date": f"{t.get('year')}-{t.get('month') or '?'}",
                "seed_investors": [
                    {"name": i["investor"]["name"], "uuid": i["investor"]["uuid"], "is_lead": i.get("is_lead")}
                    for i in investors
                ],
            }
    return {
        "filter": filt,
        "total_matches": total,
        "fetched": len(rows),
        "truncated": truncated,
        "companies": companies,
    }


def get_recent_seed_cohort(location_id: int, tag_ids: list[int], months_back: int = 18):
    """Seed rounds in roughly the last N months, computed from today's date.
    Dealroom's `date` filter only accepts year or year-month (no day) — see NOTES.md.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=months_back * 30.44)
    cutoff_ym = f"{cutoff.year:04d}-{cutoff.month:02d}"
    tag_leg = _tag_filter_leg(tag_ids)
    filt = with_legs("standardized_round[eq]:Seed", f"date[gte]:{cutoff_ym}",
                     f"hq_location[eq]:{location_id}", tag_leg, VC_DEFAULT_LEGS)
    rows, total, truncated = paginate(filt=filt, path="/data/transactions", sort="-date")

    companies: dict[str, dict] = {}
    for t in rows:
        c = t["company"]
        u = c["uuid"]
        if u not in companies:
            companies[u] = {
                "uuid": u,
                "name": c["name"],
                "domain": c.get("domain"),
                "company_obj": c,
                "seed_round_date": f"{t.get('year')}-{t.get('month') or '?'}",
                "momentum": momentum_score(c),
            }
    return {
        "filter": filt,
        "cutoff_date": cutoff.isoformat(),
        "total_matches": total,
        "fetched": len(rows),
        "truncated": truncated,
        "companies": companies,
    }


def get_stage_cohort_uuids(location_id: int, tag_ids: list[int], round_values: list[str]):
    tag_leg = _tag_filter_leg(tag_ids)
    if len(round_values) == 1:
        round_leg = f"standardized_round[eq]:{round_values[0]}"
    else:
        round_leg = "standardized_round[in_any]:" + "|".join(round_values)
    filt = f"and(hq_location[eq]:{location_id},{tag_leg},{round_leg})"
    rows, total, truncated = paginate(filt=filt, path="/data/transactions")
    uuids = set(t["company"]["uuid"] for t in rows)
    return uuids, total, truncated


def analyze_thesis(location_id: int, tag_ids: list[int], fund_uuid: str | None,
                    year_from: int = 2019, year_to: int = 2020):
    seed = get_seed_cohort(location_id, tag_ids, year_from, year_to)
    sa_uuids, sa_total, sa_truncated = get_stage_cohort_uuids(location_id, tag_ids, ["Series A"])
    sb_uuids, sb_total, sb_truncated = get_stage_cohort_uuids(
        location_id, tag_ids, ["Series B", "Series C", "Series D", "Series E", "Growth"]
    )

    results = []
    n_reached_a = 0
    n_reached_b_plus = 0
    n_fund_backed = 0

    for u, info in seed["companies"].items():
        reached_a = u in sa_uuids
        reached_b_plus = u in sb_uuids
        if reached_a:
            n_reached_a += 1
        if reached_b_plus:
            n_reached_b_plus += 1

        is_exited, high_valuation, val_amount = score_company(info["company_obj"])
        round_tier_score = 30 if reached_b_plus else (10 if reached_a else 0)
        score = round_tier_score + (100 if high_valuation else 0) + (20 if is_exited else 0)

        fund_backed = bool(fund_uuid) and any(
            inv["uuid"] == fund_uuid for inv in info["seed_investors"]
        )
        if fund_backed:
            n_fund_backed += 1

        results.append({
            "uuid": u,
            "name": info["name"],
            "domain": info["domain"],
            "seed_round_date": info["seed_round_date"],
            "reached_series_a": reached_a,
            "reached_series_b_plus": reached_b_plus,
            "is_exited": is_exited,
            "is_1bn_plus_valuation": high_valuation,
            "current_valuation": val_amount,
            "score": score,
            "seed_investors": [i["name"] for i in info["seed_investors"]],
            "fund_backed_at_seed": fund_backed,
        })

    results.sort(key=lambda r: (r["score"], r["current_valuation"] or 0), reverse=True)

    n_companies = len(seed["companies"])
    pct_reached_a_or_later = (
        round(100 * sum(1 for r in results if r["reached_series_a"] or r["reached_series_b_plus"]) / n_companies, 1)
        if n_companies else 0
    )

    winners = [r for r in results if r["score"] > 0]
    ones_that_got_away = [r for r in winners if not r["fund_backed_at_seed"]][:10]

    return {
        "location_id": location_id,
        "tag_ids": tag_ids,
        "year_from": year_from,
        "year_to": year_to,
        "seed_filter": seed["filter"],
        "seed_total_matches": seed["total_matches"],
        "seed_truncated": seed["truncated"],
        "unique_companies": n_companies,
        "series_a_cohort_total": sa_total,
        "series_a_truncated": sa_truncated,
        "series_b_plus_cohort_total": sb_total,
        "series_b_plus_truncated": sb_truncated,
        "pct_reached_series_a_or_later": pct_reached_a_or_later,
        "fund_uuid": fund_uuid,
        "fund_backed_count": n_fund_backed,
        "fund_performance_card": {
            "companies_backed_at_seed": n_fund_backed,
            "of_those_still_scoring_a_win": sum(
                1 for r in results if r["fund_backed_at_seed"] and r["score"] > 0
            ),
            "best_pick": next(
                (r for r in results if r["fund_backed_at_seed"]), None
            ),
        } if fund_uuid else None,
        "top_5_winners": results[:5],
        "ones_that_got_away": ones_that_got_away,
        "all_companies": results,
    }


def analyze_today(location_id: int, tag_ids: list[int], months_back: int = 18, top_n: int = 15):
    cohort = get_recent_seed_cohort(location_id, tag_ids, months_back=months_back)
    ranked = sorted(cohort["companies"].values(), key=lambda c: c["momentum"], reverse=True)
    ranked_out = [
        {
            "uuid": c["uuid"],
            "name": c["name"],
            "domain": c["domain"],
            "seed_round_date": c["seed_round_date"],
            "momentum_score": c["momentum"],
            "employee_count_1y_growth": c["company_obj"].get("employee_count_1y_growth"),
            "traffic_growth_3m": (c["company_obj"].get("traffic") or {}).get("growth_3m"),
            "is_hiring": (c["company_obj"].get("jobs") or {}).get("is_hiring"),
            "open_roles": (c["company_obj"].get("jobs") or {}).get("open_count"),
        }
        for c in ranked[:top_n]
    ]
    return {
        "location_id": location_id,
        "tag_ids": tag_ids,
        "cutoff_date": cohort["cutoff_date"],
        "filter": cohort["filter"],
        "total_matches": cohort["total_matches"],
        "truncated": cohort["truncated"],
        "unique_companies": len(cohort["companies"]),
        "ranked_by_momentum": ranked_out,
    }
