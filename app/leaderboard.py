"""
"Who backed this thesis best": ranks the venture firms that invested in a
thesis's cohort, by what became of the companies they backed.

Built entirely from data the backtest already has -- the investors listed on
the cohort's own rounds, pooled across every matched round of each company --
plus one small lookup per candidate firm to ask Dealroom what KIND of investor
it is. That lookup is the filter: only firms Dealroom labels "venture capital"
are ranked. People (angels), accelerators (Y Combinator, Techstars) and
crowdfunding platforms are left out on purpose; they'd otherwise top a ranking
on sheer deal volume, and a seed-stage VC room is asking about VCs.

Limits, stated rather than hidden (the screen repeats them):
  * Dealroom lists investors on only part of its rounds, so this is "best on
    what Dealroom has recorded", not "best, full stop".
  * Every investor named on a round gets full credit for it (syndicates), so
    two firms in the same round both score it.
  * A firm needs MIN_COMPANIES companies in the cohort to be ranked at all --
    one lucky unicorn is an anecdote, not a track record.
"""
from concurrent.futures import ThreadPoolExecutor

from . import dealroom_client as dc
from . import scoring

MIN_COMPANIES = 3
CANDIDATE_CAP = 30   # firms we classify; each is one small Dealroom request
TOP_N = 10
CLASSIFY_WORKERS = 8


def _is_venture_firm(inv: dict) -> bool:
    """Ask Dealroom what this investor is. Anything we can't positively
    identify as a venture capital firm -- not found, a name that matches a
    different record, a filter-unsafe name -- is left out, never guessed in."""
    name = inv["name"]
    if not scoring.is_filter_safe_name(name):
        return False
    try:
        body = dc.api_get("/data/investors", {"filter": f"name[eq]:{name}", "limit": 1})
    except RuntimeError:
        return False
    data = body.get("data") or []
    if not data or data[0].get("uuid") != inv["uuid"]:
        return False
    return any(t.get("code") == "venture_capital" for t in (data[0].get("types") or []))


def _outcome(r: dict) -> str:
    if r["is_unicorn"]:
        return "unicorn"
    if r["reached_series_b_plus"]:
        return "series_b_plus"
    if r["reached_series_a"]:
        return "series_a"
    return "none"


def build_leaderboard(companies: dict, results: list[dict], cohort_pct_a_plus: float):
    """companies: the cohort's company dict (has matched_investors).
    results: the scored company rows from run_backtest."""
    by_uuid = {r["uuid"]: r for r in results}

    firms: dict[str, dict] = {}
    for u, info in companies.items():
        r = by_uuid.get(u)
        if r is None:
            continue
        for inv in info["matched_investors"]:
            if inv.get("type") != "organization":
                continue
            f = firms.setdefault(inv["uuid"], {"uuid": inv["uuid"], "name": inv["name"], "companies": {}})
            f["companies"][u] = r

    # Only firms with enough companies are worth a classification request.
    candidates = sorted(
        (f for f in firms.values() if len(f["companies"]) >= MIN_COMPANIES),
        key=lambda f: -len(f["companies"]),
    )[:CANDIDATE_CAP]

    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        is_vc = list(pool.map(_is_venture_firm, candidates))
    ranked = [f for f, ok in zip(candidates, is_vc) if ok]

    rows = []
    for f in ranked:
        cs = sorted(f["companies"].values(),
                    key=lambda r: (r["score"], r["latest_valuation"] or 0), reverse=True)
        n = len(cs)
        reached = sum(1 for r in cs if r["reached_series_a"] or r["reached_series_b_plus"])
        rows.append({
            "name": f["name"],
            "uuid": f["uuid"],
            "companies": n,
            "total_score": sum(r["score"] for r in cs),
            "pct_reached_series_a_or_later": round(100 * reached / n, 1),
            "unicorns": sum(1 for r in cs if r["is_unicorn"]),
            "best": [{"name": r["name"], "latest_valuation": r["latest_valuation"]}
                     for r in cs[:3] if r["score"] > 0],
            "outcomes": [_outcome(r) for r in cs],
            "winners": sum(1 for r in cs if r["reached_series_b_plus"] or r["is_unicorn"]),
            "company_uuids": [r["uuid"] for r in cs],
        })
    rows.sort(key=lambda x: (x["total_score"], x["pct_reached_series_a_or_later"]), reverse=True)

    return {
        "min_companies": MIN_COMPANIES,
        "cohort_pct_reached_series_a_or_later": cohort_pct_a_plus,
        "firms_considered": len(firms),
        "firms_ranked": len(rows),
        "top": rows[:TOP_N],
        "all_ranked_names": [x["name"] for x in rows],
        "ranked": rows,
    }
