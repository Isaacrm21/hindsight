"""
Static, pre-verified dropdown options for the "plain English thesis" feature's
fallback path. These are hardcoded on purpose — the fallback exists precisely
for when something's gone wrong upstream (no API key, Claude API down,
network trouble), so it must not itself depend on a live call succeeding.
Every ID below was looked up and confirmed against the live Dealroom API
before being hardcoded here (see session notes) — never guessed.
"""

CURATED_LOCATIONS = [
    {"id": 93, "label": "United Kingdom"},
    {"id": 233, "label": "United States"},
    {"id": 76, "label": "Europe (region)"},
    {"id": 133, "label": "Germany"},
    {"id": 433, "label": "France"},
    {"id": 973, "label": "India"},
    {"id": 423, "label": "Israel"},
    {"id": 1473, "label": "Canada"},
]

# family: 'industry' unless noted — per the project's own skill file, industry
# beats a same-named grey "sector" tag for a broad vertical like this.
CURATED_INDUSTRIES = [
    {"id": 126403, "label": "Fintech", "family": "industry"},
    {"id": 125403, "label": "Health", "family": "industry"},
    {"id": 202, "label": "AI / Deep Tech", "family": "technology"},
    {"id": 10009203, "label": "Robotics", "family": "industry"},
    {"id": 126203, "label": "Cybersecurity", "family": "industry"},
    {"id": 124403, "label": "Gaming", "family": "industry"},
    {"id": 10011103, "label": "Transportation", "family": "industry"},
    {"id": 10002203, "label": "Education", "family": "industry"},
    {"id": 127003, "label": "Real Estate", "family": "industry"},
    {"id": 10002303, "label": "Energy", "family": "industry"},
    {"id": 10001203, "label": "Media", "family": "industry"},
    {"id": 126103, "label": "Legal", "family": "industry"},
]
