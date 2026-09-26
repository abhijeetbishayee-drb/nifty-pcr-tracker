# Nifty PCR Tracker

Live weekly + monthly Put-Call Ratio tracker for Nifty, independent of any broker
account. Data comes from Sensibull's public option chain (no login required), scraped
with a headless browser since NSE's own site blocks headless automation outright
(Akamai Bot Manager).

- **Weekly PCR**: near-ATM strikes at 100-point steps.
- **Monthly PCR**: near-ATM strikes at 500-point steps.
- **Violation**: PCR below 0.7 or above 2.0 — flags the "equilibrium strike" (nearest
  Call OI ≈ Put OI) that price tends to gravitate toward.
- **0.41→0.80 crossing**: tracks each day's intraday PCR range and flags when it has
  swung from ≤0.41 up to ≥0.80 (or the reverse) within the session — a stop-loss
  signal; give it 15-30 minutes to stabilize before acting on it.

`scripts/fetch_pcr.py` runs on a schedule via GitHub Actions during NSE market hours,
writes `pcr_data.json`, and `index.html` (served via GitHub Pages) reads it and
refreshes every 30 seconds.

This is a personal trading tool, not a recommendation or financial advice. Data
provenance is a third-party mirror of NSE data, not the official exchange feed —
cross-check against NSE directly before trusting it for anything material.
