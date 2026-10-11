Project: Hindsight. A 3-hour hackathon build on the Dealroom API (base URL https://api.beta.dealroom.app — corrected from the original api-next.dealroom.co, which does not work; see NOTES.md, credentials in dealroom-isaac-rodrigues-mendes.env). The demo is 90 seconds, judged by Dealroom and a room of seed-stage VCs.

The user is not technical. Always explain in plain English, keep things simple, and never show the contents of .env.

What the app does: the user types an investment thesis. The app (1) finds every company that raised a seed round matching the thesis in 2019-2020, (2) scores what happened to each one next, (3) shows a "fund performance" card plus a "ones that got away" list (winners a named fund did not back), (4) runs the same thesis on seed rounds from the last 18 months and ranks today's matches by momentum.

Scoring ("Power Law-style", because exit and revenue data are not available in the API): reached Series A = 10 points, reached Series B or later = 30 points, latest valuation of $1bn or more = 100 points, exited = 20 points.

Technical rules: Python backend using FastAPI. One index.html page for the frontend using Tailwind and Chart.js from a CDN. The Dealroom rate limit is 5 requests per second, so throttle requests and retry on HTTP 429 using the Retry-After header. Save every API response to a /cache folder as JSON so repeat queries are instant. Create a .gitignore that excludes .env and /cache.
