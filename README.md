# Hindsight

Backtest an investment thesis against Dealroom's record.

You type a thesis in plain English ("UK seed-stage fintech in 2019 and 2020, compared to
LocalGlobe"). The app:

1. finds every round matching that thesis in the chosen window,
2. scores what happened to each company next,
3. shows the round-by-round record for the winners,
4. benchmarks a named fund's coverage of that thesis — including the winners it reached
   one round too late,
5. runs the same thesis against the last 18 months and ranks today's matches by momentum.

Built for the Dealroom London hackathon.

---

## Running it

You need Python 3.12 and your own Dealroom API credentials (see below).

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your credentials
python -m uvicorn app.main:app --port 8010
```

Then open:

- `http://localhost:8010/` — live run against the Dealroom API
- `http://localhost:8010/?demo=1` — pre-captured results, zero network calls

The `?demo=1` mode exists because the demo is judged live and venue wifi is not to be
trusted. It reads `static/demo-data.js` and makes no API requests at all.

## Credentials

**You need your own Dealroom key.** It is issued per person for the hackathon and the
terms forbid sharing or publishing it, so there is no shared key in this repo — not even
in the history. `.env.example` lists the three variables.

`app/config.py` looks for `dealroom-<name>.env` first, then `.env`. Both work.

`ANTHROPIC_API_KEY` is optional: it powers plain-English thesis parsing. Without it the
app falls back to three dropdowns (location / industry / years) and everything else works.

---

## Architecture

FastAPI backend, one static HTML page. No build step, no bundler, no framework.

| File | What it does |
|---|---|
| `app/main.py` | Routes: `/api/parse-thesis`, `/api/backtest`, `/api/momentum`, `/api/health`, `/api/dealroom-check` |
| `app/dealroom_client.py` | Rate-limited (5 req/s, thread-safe), retrying, disk-caching HTTP client |
| `app/scoring.py` | Shared pipeline: pagination, scoring, journey fetching, filter-safety checks |
| `app/backtest.py` | The backtest + fund-coverage feature |
| `app/momentum.py` | "Same thesis today", ranked by headcount and traffic momentum |
| `app/llm_thesis.py` + `app/thesis_resolver.py` | Plain English → Dealroom filter params, grounded against real search results |
| `app/curated_options.py` | Static verified fallback options for the dropdowns |
| `static/index.html` | The whole frontend |
| `static/demo-data.js` | Pre-captured demo payload. **Regenerate by re-running the pipeline — never hand-edit the numbers.** |
| `NOTES.md` | Dealroom API findings. **Read this before touching filter-building code.** |
| `previous-build/` | An earlier, fuller build. Not wired in. Reference only. |

### Scoring

Exit values and revenue aren't in the API, so outcomes are scored "Power Law style":

| Outcome | Points |
|---|---|
| Reached Series A | 10 |
| Reached Series B or later | 30 |
| Current valuation ≥ $1bn | 100 |
| Exited | 20 |

Round tiers are highest-tier-only — a Series B company scores 30, not 30 + 10.
The $1bn bonus uses the company's **current** valuation, not Dealroom's "ever was a
unicorn" classification.

---

## Things that will bite you

Most of these cost real debugging time. `NOTES.md` has the full detail.

- **Never decide "this fund didn't back that company" from the investor list attached to a
  round.** Dealroom populates it on roughly half of rounds, so absence means "unrecorded",
  not "absent". Query from the investor side instead — `investor.name[eq]:<fund>`, singular
  (`investors.…` is not a valid relationship, and `investor.uuid` does not exist). Doing it
  the naive way reported LocalGlobe as backing 0 of 175 companies when the real answer was 5.
- **The filter DSL has no escaping** for `, | ( ) [ ] :`, and real company names contain them
  ("Vaxxinity | Covaxx"). Always gate names through `scoring.is_filter_safe_name()`.
- **Rounds are dated to the month**, so a seed and the Series A that followed can share a
  date. Sort journeys by stage as a tiebreak or you will print a company raising its
  Series A before its seed.
- **Transactions carry no `traffic` field** — only `/data/companies?view=full` does.
- **There is no way to filter transactions by a list of company IDs.** The app batches
  `company.name[in_any]` against the cohort's own names instead.
- **`requestAnimationFrame` is throttled to a standstill in a backgrounded tab.** Don't put
  anything load-bearing behind it; this once rendered the entire results page at opacity 0.
- **Rate limit is 5 req/s** and it is a hard floor. Concurrency hides network latency, not
  the limit.

## Performance

Backtest and momentum run concurrently. Cold, with the cache cleared, the demo thesis
completes in well under 20 seconds end to end (measured: backtest 6.8s, momentum 9.8s,
in parallel). Every API response is cached to `cache/` as JSON, so repeat queries are
effectively instant.
