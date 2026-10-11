"""
The backtest feature.

Input: a thesis — location, industry/technology tag(s), round type, a year
range, and a fund name.

Does:
  1. Finds matching rounds of that type in that window (the "seed cohort",
     though round_type is a parameter, not hardcoded).
  2. For every matched company, checks whether it later reached Series A /
     Series B+ (via the reverse-lookup trick — Dealroom has no "filter
     transactions by a list of company IDs"; see NOTES.md) and reads its
     latest valuation / unicorn / exited status off the company record.
  3. Applies the CLAUDE.md scoring:
       reached Series A        = 10
       reached Series B+       = 30  (highest tier only, not cumulative)
       latest valuation >= $1bn = 100
       exited                  = 20
  4. For the top 10 by score, fetches their full round-by-round journey
     (seed date/amount, every later round, latest valuation) via the
     `company.name` relationship filter — precise and cheap (one small
     request per company, not a broad reverse-lookup).
  5. Returns the fund-performance numbers and the list of winners that named
     fund did not back at the matched round.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor

from . import dealroom_client as dc
from . import scoring
from .leaderboard import build_leaderboard
from .scoring import STAGE_RANK, VC_DEFAULT_LEGS, _tag_filter_leg, is_after, paginate, score_company, with_legs

# Each of these per-company/per-cohort calls is independent, so they're run
# concurrently rather than one-at-a-time — the 20-second budget doesn't work
# otherwise. The shared rate limiter in dealroom_client.py is thread-safe and
# still caps real throughput to 5 req/sec; this just removes the *dead time*
# of waiting for each request's network round trip before starting the next.
JOURNEY_FETCH_WORKERS = 10


def _resolve_fund_uuid(fund_name: str) -> str | None:
    body = dc.api_get("/data/investors", {"filter": f"name[eq]:{fund_name}", "limit": 1})
    data = body.get("data") or []
    return data[0]["uuid"] if data else None


# Dealroom records an investor list on only about HALF of its rounds (see
# NOTES.md: 24 of a 50-round sample had any investor at all). So deciding
# "this fund didn't back this company" from the investor list embedded in
# the cohort's own transaction rows silently turns a MISSING RECORD into an
# accusation -- live, that reported LocalGlobe as backing 0 of 175 scoring
# UK-fintech-seed companies when Dealroom in fact has them on 7 rounds in
# exactly that cohort (Yapily, Xelix, Laka, Radix, Swipii), two of which
# reached Series B. Presenting that as "you missed all of them" to the fund
# in question is worse than useless; it's wrong.
#
# The authoritative question is asked from the investor's side instead:
# filter transactions by investor.name and read back which companies they
# actually appear on. Two queries:
#   - at_matched_stage: the same cohort filter + this investor -> the fund
#     genuinely backed this company AT the matched round.
#   - any_stage: the same location/industry, ALL rounds + this investor ->
#     the fund backed the company, but at some other (usually later) round.
# The second one is what surfaces the most interesting case in the whole
# app: the fund that found a winner one round too late.
def _fund_positions(fund_name: str, location_id: int, tag_ids: list[int],
                    round_type: str, year_from: int, year_to: int):
    tag_leg = _tag_filter_leg(tag_ids)
    inv_leg = f"investor.name[eq]:{fund_name}"

    at_filt = with_legs(inv_leg, f"standardized_round[eq]:{round_type}", f"year[gte]:{year_from}",
                        f"year[lte]:{year_to}", f"hq_location[eq]:{location_id}", tag_leg, VC_DEFAULT_LEGS)
    any_filt = f"and({inv_leg},hq_location[eq]:{location_id},{tag_leg})"

    with ThreadPoolExecutor(max_workers=2) as pool:
        at_future = pool.submit(paginate, filt=at_filt, path="/data/transactions", sort="-date")
        any_future = pool.submit(paginate, filt=any_filt, path="/data/transactions", sort="date")
        at_rows, _, _ = at_future.result()
        any_rows, _, _ = any_future.result()

    backed_at_matched = {t["company"]["uuid"] for t in at_rows}

    # Earliest round this fund took in each company, for the "came in at
    # Series A, not seed" line. any_rows is sorted ascending by date, so the
    # first row seen per company IS the fund's entry point.
    entry_round: dict[str, dict] = {}
    for t in any_rows:
        u = t["company"]["uuid"]
        if u not in entry_round:
            entry_round[u] = {
                "round": t.get("standardized_round") or t.get("round_type"),
                "year": t.get("year"),
            }
    return backed_at_matched, entry_round


def _get_cohort(location_id: int, tag_ids: list[int], round_type: str, year_from: int, year_to: int,
                max_pages: int = scoring.MAX_PAGES):
    filt = with_legs(f"standardized_round[eq]:{round_type}", f"year[gte]:{year_from}", f"year[lte]:{year_to}",
                     f"hq_location[eq]:{location_id}", _tag_filter_leg(tag_ids), VC_DEFAULT_LEGS)
    rows, total, truncated = paginate(filt=filt, path="/data/transactions", sort="-date", max_pages=max_pages)

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
                "matched_round_date": f"{t.get('year')}-{t.get('month') or '?'}",
                "matched_year": t.get("year"),
                "matched_month": t.get("month"),
                "matched_round_amount": t.get("amount"),
                "matched_investors": [],
                "entry": {"year": t.get("year"), "month": t.get("month"), "stage": t.get("standardized_round")},
            }
        # Rows arrive newest first, so the last one seen is the company's first matched round
        companies[u]["entry"] = {"year": t.get("year"), "month": t.get("month"), "stage": t.get("standardized_round")}
        # A company can have two matched rounds (second close, follow-on seed),
        # and the investor list may sit on either. Pool them across ALL of the
        # company's matched rows, or an investor on the second round is lost --
        # which matters now the leaderboard is built from these lists.
        have = {i["uuid"] for i in companies[u]["matched_investors"]}
        for i in t.get("investors") or []:
            inv = i["investor"]
            if inv["uuid"] not in have:
                have.add(inv["uuid"])
                companies[u]["matched_investors"].append(
                    {"name": inv["name"], "uuid": inv["uuid"], "is_lead": i.get("is_lead"),
                     "type": inv.get("type")}
                )
    return {"filter": filt, "total_matches": total, "fetched": len(rows), "truncated": truncated, "companies": companies}


NAME_BATCH_SIZE = 300  # company.name[in_any] pipe-batch size; ~6-7k chars/request, well under the
# ~8.8k limit already confirmed working live (see NOTES.md) -- fewer, bigger batches means fewer
# requests, which matters because the 5 req/sec Dealroom limit is shared and genuinely the floor
STAGE_CHECK_WORKERS = 10


def _reached_stage_uuids(company_names: list[str], location_id: int, tag_ids: list[int],
                          round_values: list[str], entries: dict):
    """Which of THESE companies (by name) ever reached one of these round
    stages — not 'every company in the vertical that ever reached this
    stage' (the old approach). For a broad thesis the vertical's full
    history can be tens of thousands of rows; restricting to company.name
    batches of our own cohort, instead of paginating the whole population,
    is what keeps a big thesis inside the 20-second budget.

    Company names containing filter-syntax characters (commas, pipes,
    parens — see scoring.is_filter_safe_name) can't go into this batched
    query at all, so they're excluded rather than corrupting the whole
    batch; the count excluded is returned so it's a reported gap, not a
    silent one — those specific companies default to 'not reached' for
    this stage rather than causing a failure."""
    if not company_names:
        return set(), 0, False, 0

    safe_names = [n for n in company_names if scoring.is_filter_safe_name(n)]
    skipped_unsafe = len(company_names) - len(safe_names)
    if not safe_names:
        return set(), 0, False, skipped_unsafe

    tag_leg = _tag_filter_leg(tag_ids)
    round_leg = (
        f"standardized_round[eq]:{round_values[0]}" if len(round_values) == 1
        else "standardized_round[in_any]:" + "|".join(round_values)
    )
    batches = [safe_names[i:i + NAME_BATCH_SIZE] for i in range(0, len(safe_names), NAME_BATCH_SIZE)]

    def fetch_batch(names):
        name_leg = "company.name[in_any]:" + "|".join(names)
        filt = with_legs(name_leg, f"hq_location[eq]:{location_id}", tag_leg, round_leg)
        body = dc.api_get("/data/transactions", {"filter": filt, "limit": 500, "include_total": "true"})
        return body

    with ThreadPoolExecutor(max_workers=STAGE_CHECK_WORKERS) as pool:
        bodies = list(pool.map(fetch_batch, batches))

    uuids = set()
    truncated = False
    for body in bodies:
        for t in body.get("data", []):
            entry = entries.get(t["company"]["uuid"])
            # Only a later, higher-stage round counts, so a Series A cohort cannot "reach" Series A by its own round
            if entry and is_after(t, entry) and STAGE_RANK.get(t.get("standardized_round"), -1) > STAGE_RANK.get(entry["stage"], -1):
                uuids.add(t["company"]["uuid"])
        total = body["page"].get("total")
        if total is not None and total > len(body.get("data", [])):
            truncated = True  # a single company had >500 rounds of this stage -- vanishingly unlikely, but reported not hidden

    return uuids, len(uuids), truncated, skipped_unsafe


def _is_four_x_estimate(value, amount) -> bool:
    # Dealroom fills an undisclosed post-money valuation as exactly 4x the round amount
    return bool(value and amount and abs(value - 4 * amount) <= 10 + value * 1e-5)


def _constellation_point(r: dict, info: dict) -> dict:
    company = info["company_obj"]
    valuation = company.get("valuation") or {}
    value = valuation.get("value")
    known_amounts = [info["matched_round_amount"]] + [j.get("amount") for j in r.get("journey") or []]
    return {
        "uuid": r["uuid"],
        "name": r["name"],
        "image": company.get("image"),
        "dealroom_url": company.get("dealroom_url"),
        "matched_year": info["matched_year"],
        "matched_month": info["matched_month"],
        "matched_round_amount": info["matched_round_amount"],
        "valuation": value,
        "valuation_year": valuation.get("year"),
        "valuation_month": valuation.get("month"),
        "valuation_estimated": any(_is_four_x_estimate(value, a) for a in known_amounts),
        "reached_series_a": r["reached_series_a"],
        "reached_series_b_plus": r["reached_series_b_plus"],
        "is_unicorn": r["is_unicorn"],
        "is_exited": r["is_exited"],
        "fund_backed": r["fund_backed"],
        "fund_backed_later": r["fund_backed_later"],
        "fund_entry_round": r["fund_entry_round"],
    }


B_PLUS = ["Series B", "Series C", "Series D", "Series E", "series_f_", "Growth", "IPO"]
BASELINE_SAMPLE = 3000


def _market_baseline(location_id: int, round_type: str, year_from: int, year_to: int):
    cohort = _get_cohort(location_id, [], round_type, year_from, year_to, max_pages=60)
    companies = cohort["companies"]
    # A fixed pseudo-random sample keeps a big market fast and gives the same answer every run
    keep = sorted(companies, key=lambda u: hashlib.sha1(u.encode()).hexdigest())[:BASELINE_SAMPLE]
    names = [companies[u]["name"] for u in keep]
    entries = {u: companies[u]["entry"] for u in keep}
    with ThreadPoolExecutor(max_workers=2) as pool:
        sa = pool.submit(_reached_stage_uuids, names, location_id, [], ["Series A"], entries)
        sb = pool.submit(_reached_stage_uuids, names, location_id, [], B_PLUS, entries)
        sa_uuids, sb_uuids = sa.result()[0], sb.result()[0]
    unicorn = {u for u in keep if score_company(companies[u]["company_obj"])[1]}
    n = len(keep)
    share = lambda count: round(100 * count / n, 1) if n else None
    b_plus = sum(1 for u in keep if u in sb_uuids and u not in unicorn)
    a_only = sum(1 for u in keep if u in sa_uuids and u not in sb_uuids and u not in unicorn)
    return {
        "population": len(companies),
        "sampled": n,
        "population_truncated": cohort["truncated"],
        "pct_reached_series_a_or_later": share(sum(1 for u in keep if u in sa_uuids or u in sb_uuids)),
        "pct_unicorn": share(len(unicorn)),
        "breakdown_pct": {
            "billion_plus": share(len(unicorn)),
            "series_b_plus": share(b_plus),
            "series_a_only": share(a_only),
            "no_later_round": share(n - len(unicorn) - b_plus - a_only),
        },
    }


def run_backtest(location_id: int, tag_ids: list[int], round_type: str,
                  year_from: int, year_to: int, fund_name: str | None):
    baseline_pool = ThreadPoolExecutor(max_workers=1)
    baseline_future = baseline_pool.submit(_market_baseline, location_id, round_type, year_from, year_to)
    # The fund lookups and the cohort query are independent -- fire together.
    with ThreadPoolExecutor(max_workers=3) as pool:
        fund_future = pool.submit(_resolve_fund_uuid, fund_name) if fund_name else None
        positions_future = pool.submit(
            _fund_positions, fund_name, location_id, tag_ids, round_type, year_from, year_to
        ) if fund_name else None
        cohort_future = pool.submit(_get_cohort, location_id, tag_ids, round_type, year_from, year_to)
        fund_uuid = fund_future.result() if fund_future else None
        backed_at_matched, fund_entry_round = positions_future.result() if positions_future else (set(), {})
        cohort = cohort_future.result()

    # The stage checks need the cohort's company names first (see
    # _reached_stage_uuids), so they can't start until the cohort above is
    # in -- but they're independent of EACH OTHER, so fire together.
    company_names = [info["name"] for info in cohort["companies"].values()]
    entries = {u: info["entry"] for u, info in cohort["companies"].items()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        sa_future = pool.submit(_reached_stage_uuids, company_names, location_id, tag_ids, ["Series A"], entries)
        sb_future = pool.submit(_reached_stage_uuids, company_names, location_id, tag_ids, B_PLUS, entries)
        sa_uuids, sa_total, sa_truncated, sa_skipped = sa_future.result()
        sb_uuids, sb_total, sb_truncated, sb_skipped = sb_future.result()
    name_lookup_skipped = max(sa_skipped, sb_skipped)  # same excluded set either way

    results = []
    for u, info in cohort["companies"].items():
        reached_a = u in sa_uuids
        reached_b_plus = u in sb_uuids
        is_exited, high_valuation, val_amount = score_company(info["company_obj"])
        round_tier_score = 30 if reached_b_plus else (10 if reached_a else 0)
        score = round_tier_score + (100 if high_valuation else 0) + (20 if is_exited else 0)
        # Authoritative (see _fund_positions) -- NOT inferred from the
        # round's own investor list, which Dealroom populates only ~half
        # the time.
        fund_backed = u in backed_at_matched
        entry = fund_entry_round.get(u)
        fund_backed_later = bool(entry) and not fund_backed

        results.append({
            "uuid": u,
            "name": info["name"],
            "domain": info["domain"],
            "matched_round_date": info["matched_round_date"],
            "matched_round_amount": info["matched_round_amount"],
            "reached_series_a": reached_a,
            "reached_series_b_plus": reached_b_plus,
            "is_exited": is_exited,
            "is_unicorn": high_valuation,
            "latest_valuation": val_amount,
            "score": score,
            "matched_round_investors": [i["name"] for i in info["matched_investors"]],
            "fund_backed": fund_backed,
            "fund_backed_later": fund_backed_later,
            "fund_entry_round": entry["round"] if fund_backed_later else None,
            "fund_entry_year": entry["year"] if fund_backed_later else None,
        })

    results.sort(key=lambda r: (r["score"], r["latest_valuation"] or 0), reverse=True)

    n_companies = len(cohort["companies"])
    pct_reached_a_or_later = (
        round(100 * sum(1 for r in results if r["reached_series_a"] or r["reached_series_b_plus"]) / n_companies, 1)
        if n_companies else 0
    )
    unicorn_count = sum(1 for r in results if r["is_unicorn"])
    # Mutually exclusive outcome buckets for the cohort chart -- each company
    # counted once, at its best outcome, so the four buckets sum to the cohort.
    outcome_breakdown = {
        "billion_plus": unicorn_count,
        "series_b_plus": sum(1 for r in results if r["reached_series_b_plus"] and not r["is_unicorn"]),
        "series_a_only": sum(
            1 for r in results
            if r["reached_series_a"] and not r["reached_series_b_plus"] and not r["is_unicorn"]
        ),
    }
    outcome_breakdown["no_later_round"] = n_companies - sum(outcome_breakdown.values())
    total_score = sum(r["score"] for r in results)

    top_10 = results[:10]
    with ThreadPoolExecutor(max_workers=JOURNEY_FETCH_WORKERS) as pool:
        journeys = list(pool.map(
            lambda r: scoring.fetch_company_journey(r["name"], location_id, tag_ids), top_10
        ))
    for r, journey in zip(top_10, journeys):
        r["journey"] = journey["rounds"]
        r["journey_ambiguous"] = journey["ambiguous"]

    constellation = [_constellation_point(r, cohort["companies"][r["uuid"]]) for r in results]

    scoring_companies = [r for r in results if r["score"] > 0]
    ones_that_got_away = [r for r in scoring_companies if not r["fund_backed"]]
    # The most interesting slice in the whole app: winners this fund DID
    # find, but one or more rounds after the seed it could have led.
    found_too_late = [r for r in ones_that_got_away if r["fund_backed_later"]]

    leaderboard = build_leaderboard(cohort["companies"], results, pct_reached_a_or_later)
    try:
        baseline = baseline_future.result()
    except RuntimeError:
        baseline = None
    baseline_pool.shutdown(wait=False)

    return {
        "leaderboard": leaderboard,
        "baseline": baseline,
        "inputs": {
            "location_id": location_id, "tag_ids": tag_ids, "round_type": round_type,
            "year_from": year_from, "year_to": year_to, "fund_name": fund_name, "fund_uuid": fund_uuid,
        },
        "cohort_filter": cohort["filter"],
        "total_matches": cohort["total_matches"],
        "unique_companies": n_companies,
        "cohort_truncated": cohort["truncated"],
        "name_lookup_skipped": name_lookup_skipped,
        "series_a_cohort_total": sa_total,
        "series_b_plus_cohort_total": sb_total,
        "pct_reached_series_a_or_later": pct_reached_a_or_later,
        "unicorn_count": unicorn_count,
        "outcome_breakdown": outcome_breakdown,
        "total_score": total_score,
        "top_10_winners": top_10,
        "scoring_companies_total": len(scoring_companies),
        "fund_backed_at_matched_total": sum(1 for r in scoring_companies if r["fund_backed"]),
        "fund_backed_companies": [
            {"name": r["name"], "score": r["score"], "latest_valuation": r["latest_valuation"]}
            for r in scoring_companies if r["fund_backed"]
        ],
        "found_too_late": found_too_late[:8],
        "companies": constellation,
        "ones_that_got_away": ones_that_got_away,
    }
