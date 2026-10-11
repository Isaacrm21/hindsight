# Hindsight — Data test findings

Tested against the live Dealroom API before building anything. Short answer up front, then the plain-English detail, then the exact technical requests that worked.

**Bottom line: the idea is workable.** Every piece of data the app needs is available. There are a few real nuances below that change *how* we should build it, but nothing that breaks the concept.

One correction to flag: [CLAUDE.md](CLAUDE.md) lists the base URL as `https://api-next.dealroom.co`. The URL that actually works (confirmed live, and matches Dealroom's own API spec) is **`https://api.beta.dealroom.app`**. I'll use that when we build.

---

## 1. Can one request filter by year, round type, location AND industry?

**Yes.** Dealroom's transactions list supports combining all four in a single filter expression — confirmed against their live "filter registry" (the list of things you're allowed to filter on).

- United Kingdom location ID: **93**
- Fintech industry tag ID: **126403**
- "Artificial Intelligence" tag ID: **202** — but note, it's filed under Dealroom's "technology" category, not their "industry" category. Fintech and AI aren't quite the same *kind* of tag in their system (industry = what a company sells, e.g. Fintech, Healthtech; technology = what it's built with, e.g. AI, Blockchain). Doesn't stop us combining them in one query — just means when the app's thesis-matcher sees "AI," it should search the technology list, not the industry list.

## 2. UK fintech seed rounds, 2019–2020

**662 rounds match.** I pulled a sample and checked the investor data closely, since that's central to the "fund performance" feature.

- Investor names are included, and so is a flag for "did this investor lead the round" — but only for **about half** of rounds. In my 50-round sample, 24 had any investor listed at all, and only 7 had a lead clearly flagged. This isn't an access problem with our key — it's just that not every round in Dealroom's own dataset has that detail recorded. **The app needs to handle "investor unknown" gracefully**, and the "ones that got away" feature will necessarily be checking against a partial investor record, not a complete one.

## 3. Getting later funding rounds + latest valuation for a batch of companies

**Two separate tricks needed here, both confirmed working:**

- **Latest valuation, funding total, headcount, etc.** — these live directly on the company record, and you *can* fetch many companies in one request. I confirmed 200 companies' full records in a single call (see timing in Q6). There's no need to request them one at a time.
- **"Did this company later reach Series A / B+ / exit?"** — this is the one genuine wrinkle. You can't ask the transactions list for "just these 200 companies' rounds" directly; Dealroom's API doesn't support filtering transactions by a specific list of company IDs. The workaround (tested and it works): instead of asking per-company, ask the opposite question — "show me every UK fintech company that ever reached Series A" (one request, same location+industry filter, no year limit) — and then match the results back to our list by company ID. I tested this on a 50-company sample and correctly found 9 that had gone on to raise a Series A.

## 4. Momentum signals: headcount, web traffic, job openings

**All three exist, tested live on real companies — but they're snapshots, not full histories.** This is worth knowing because CLAUDE.md talks about "momentum" as if there's a trend line to plot:

- Headcount: current employee count, plus % growth over the last 12 months. Not a month-by-month history.
- Web traffic: current monthly visits, plus % growth over the last 3 months. Same deal — a snapshot and a short trend, not a chart-able history.
- Job openings: current open roles and a hiring yes/no. No history at all — it's "right now" only.

For the "momentum" ranking in step 4 of the app, we'll be ranking on these growth percentages and current activity, not drawing a graph over time — Dealroom simply doesn't expose that.

## 5. Investor IDs

Found all three by exact name lookup:

| Fund | Dealroom ID |
|---|---|
| LocalGlobe | `7d3c9943-e634-4481-848a-80bcf452c848` |
| Seedcamp | `a9d89ae3-22df-49b8-9fb6-57ad8701973d` |
| Index Ventures | `d8795151-5792-40da-8cd9-8ede02bc58de` |

Small gotcha: searching "Index Ventures" also returned an unrelated "Yale Index Ventures" entity, even though I asked for an exact match. Worth double-checking any investor name lookup by eye before trusting it in the app.

### 5a. Never decide "the fund missed it" from the round's own investor list

This is the most dangerous data gap in the whole API for this app. Dealroom
records an investor list on only about **half** its rounds (section 1 above:
24 of a 50-round sample). Working out whether a fund backed a company by
looking at the investors attached to each round therefore turns a *missing
record* into an *accusation*.

Live proof: doing it that way reported **LocalGlobe as backing 0 of 175**
scoring companies in the UK-fintech-seed-2019/20 cohort. Dealroom in fact has
LocalGlobe on **7 rounds in exactly that cohort** — Yapily, Xelix, Laka,
Radix, Swipii — two of which reached Series B.

Ask the question from the investor's side instead. `/data/transactions`
supports an `investor` relationship, and filtering on it is authoritative:

```
GET /data/transactions?filter=and(investor.name[eq]:LocalGlobe,standardized_round[eq]:Seed,year[gte]:2019,year[lte]:2020,hq_location[eq]:93,taxonomy_id[eq]:126403)
```

Note the field is `investor.name`, singular — `investors.…` returns
`Unknown relationship 'investors' for source 'funding-rounds'`, and
`investor.uuid` doesn't exist either (the relationship exposes `name`,
`location`, `classification`, `roles`, `total_invested` and similar, not
`uuid`). Dropping the round/year legs and keeping only location + industry
gives every position the fund holds in the vertical, which is how the app
finds the winners a fund reached **one round late** (LocalGlobe entered
Copper at Series A in 2020; Copper is worth $2bn today).

### 5b. Company `signal_rating`

`/data/companies?view=full` returns Dealroom's own proprietary
`signal_rating` per company (Yapily: 87.46), alongside
`employee_count_1y_growth`. Not currently used by the app, but it's a
stronger ranking input than raw headcount growth if the momentum feature is
ever revisited.

## 6. How long would a ~200-company test take?

**If built the smart way (batching), well under a minute of API time.** I actually ran this, not just estimated it:

- Fetching full details (valuation, funding, headcount, traffic, jobs) for 200 real companies **in one request** took **3.8 seconds**.
- The 5-requests-per-second limit turned out not to be the real bottleneck — each individual request takes **roughly 1.3–4 seconds** to come back regardless, so it's ordinary internet/server speed that sets the pace, not Dealroom's rate limit.
- A full run (pulling the seed rounds, batch-fetching the 200 companies, checking who reached Series A/B+/exit, looking up a few investors) is realistically **10–15 requests total**, so on the order of **20–45 seconds** end to end.
- The trap to avoid: if the app asked for each company one at a time instead of batching, 200 requests at ~1.3s each would take **4+ minutes** and burn far more of the rate-limited quota than it needs to. Batching isn't optional — it's the difference between a 90-second demo working and not.

---

## Exact technical requests that worked

Base URL: `https://api.beta.dealroom.app`
Every request needs both `Authorization: Bearer <token>` and `X-Client-Id: <client id>` headers. Token comes from `POST https://accounts.dealroom.co/oauth/token` (see [SETUP.txt](SETUP.txt)), lasts 24h, cache it.

**Filter registry (find valid filter keys for a given scope):**
```
GET /reference/filters?scope=transactions
GET /reference/filters?scope=companies
```

**Look up a location/industry/investor ID by name:**
```
GET /reference/filters/search?scope=transactions&q=United Kingdom&limit=10
GET /reference/filters/search?scope=transactions&q=fintech&limit=10
```

**UK fintech seed rounds, 2019–2020 (year + round type + location + industry, all at once):**
```
GET /data/transactions?filter=and(standardized_round[eq]:Seed,year[gte]:2019,year[lte]:2020,hq_location[eq]:93,taxonomy_id[eq]:126403)&limit=50&include_total=true&sort=-date
```
Paginate with the `page.next_cursor` value from the response, passed back as `&cursor=...`.

**Batch-fetch many companies' current data in one call** (up to 500 per request; use `or(...)` chains, not `in_any` with commas — that returned a 400 error when tested):
```
GET /data/companies?filter=or(id[eq]:<uuid-1>,id[eq]:<uuid-2>,...)&limit=200&view=full
```

**Find companies that later reached Series A (to match against a seed cohort by company ID):**
```
GET /data/transactions?filter=and(hq_location[eq]:93,taxonomy_id[eq]:126403,standardized_round[eq]:Series A)&limit=500&include_total=true
```
Swap `standardized_round[eq]:Series A` for `Series B`, `Series C`, etc. (or use `in_any` with those values) to cover "Series B or later." `is_exit[eq]:true` on the transactions filter, or `classification[in_any]:exited` on the companies filter, covers exits. `classification[in_any]:unicorn` or `latest_valuation[gte]:1000000000` on the companies filter covers the $1bn+ valuation tier.

**Look up an investor by exact name:**
```
GET /data/investors?filter=name[eq]:Seedcamp&limit=5
```

All requests above were run for real against the live API today and returned real data — nothing here is guessed from documentation alone.
