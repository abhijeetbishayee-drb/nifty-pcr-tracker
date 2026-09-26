"""Shared, dependency-free PCR computation logic used by both fetch_pcr.py
(live, needs Playwright) and backfill_history.py (historical, stdlib only)."""

SYMBOLS = {
    "nifty": {"tradingsymbol": "NIFTY", "weeklyStep": 100, "monthlyStep": 500, "hasWeekly": True},
    # NSE discontinued BANKNIFTY weekly expiries in 2023 -- monthly only.
    "bankNifty": {"tradingsymbol": "BANKNIFTY", "monthlyStep": 500, "hasWeekly": False},
}
BAND = 5  # strikes each side of ATM, at the symbol's step spacing

VIOLATION_LOW = 0.7
VIOLATION_HIGH = 2.0
CROSS_LOW = 0.41
CROSS_HIGH = 0.80


def select_band(rows, spot, step, band):
    if not rows:
        return []
    atm = round(spot / step) * step
    wanted = [atm + i * step for i in range(-band, band + 1)]
    return [s for s in wanted if s in rows]


def compute_pcr(rows, strikes):
    call_total = sum(rows[s]["callOI"] for s in strikes)
    put_total = sum(rows[s]["putOI"] for s in strikes)
    if call_total <= 0:
        return None
    return put_total / call_total


def equilibrium_strike(rows, strikes):
    return min(strikes, key=lambda s: abs(rows[s]["callOI"] - rows[s]["putOI"]))
