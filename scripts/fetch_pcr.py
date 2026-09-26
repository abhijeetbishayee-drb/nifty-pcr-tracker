import json
import os
import re
import sys
from datetime import datetime, date, timezone

from playwright.sync_api import sync_playwright

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_state.json")
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_data.json")

WEEKLY_STEP = 100
WEEKLY_BAND = 5   # strikes each side of ATM, at WEEKLY_STEP spacing
MONTHLY_STEP = 500
MONTHLY_BAND = 5  # strikes each side of ATM, at MONTHLY_STEP spacing

VIOLATION_LOW = 0.7
VIOLATION_HIGH = 2.0
CROSS_LOW = 0.41
CROSS_HIGH = 0.80

# Wide-viewport combined layout renders one row as:
# call_chg_pct, call_oi(lakh), call_ltp, strike, iv, put_ltp, put_oi(lakh), put_chg_pct
ROW_RE = re.compile(
    r"(-?[\d.]+%)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(\d+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(-?[\d.]+%)"
)


def month_key(d):
    return (d.year, d.month)


def parse_expiry_label(label, today):
    # label like "29 Sep" possibly followed by other text already stripped by caller
    dt = datetime.strptime(label + f" {today.year}", "%d %b %Y").date()
    if dt < today:
        dt = dt.replace(year=today.year + 1)
    return dt


def get_expiries(page, today):
    page.get_by_role("combobox").first.click(timeout=5000)
    page.wait_for_selector('[role="option"]', timeout=5000)
    labels = page.locator('[role="option"]').all_inner_texts()
    dates = []
    for label in labels:
        # "29 Sep (3 Days)" -> "29 Sep"
        m = re.match(r"(\d{1,2} [A-Za-z]{3})", label.strip())
        if m:
            dates.append(parse_expiry_label(m.group(1), today))
    page.keyboard.press("Escape")
    return sorted(set(dates))


def pick_weekly_and_monthly(expiries):
    weekly = expiries[0]
    current_month = month_key(weekly)
    same_month = [d for d in expiries if month_key(d) == current_month]
    monthly = max(same_month)
    return weekly, monthly


def fetch_chain_for_expiry(page, expiry_date):
    url = f"https://web.sensibull.com/option-chain?tradingsymbol=NIFTY&expiry={expiry_date.isoformat()}"
    page.goto(url, timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")

    spot_match = re.search(r"NIFTY\s*\n?\s*([\d,]+\.\d+)", text)
    spot = float(spot_match.group(1).replace(",", "")) if spot_match else None

    rows = {}
    for m in ROW_RE.finditer(text):
        (_call_chg_pct, call_oi, _call_ltp, strike, _iv,
         _put_ltp, put_oi, _put_chg_pct) = m.groups()
        rows[int(strike)] = {"callOI": float(call_oi), "putOI": float(put_oi)}
    return spot, rows


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
    best = min(strikes, key=lambda s: abs(rows[s]["callOI"] - rows[s]["putOI"]))
    return best


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def update_crossing_state(state, key, pcr, today_str):
    day_state = state.get(key, {})
    if day_state.get("date") != today_str:
        day_state = {"date": today_str, "min": pcr, "max": pcr, "crossed": False}
    else:
        day_state["min"] = min(day_state["min"], pcr)
        day_state["max"] = max(day_state["max"], pcr)
    if day_state["min"] <= CROSS_LOW and day_state["max"] >= CROSS_HIGH:
        day_state["crossed"] = True
    state[key] = day_state
    return day_state["crossed"]


def build_timeframe_result(rows, spot, step, band, state, key, today_str):
    strikes = select_band(rows, spot, step, band)
    if not strikes:
        return None
    pcr = compute_pcr(rows, strikes)
    if pcr is None:
        return None
    violation = pcr < VIOLATION_LOW or pcr > VIOLATION_HIGH
    eq_strike = equilibrium_strike(rows, strikes) if violation else None
    crossed = update_crossing_state(state, key, pcr, today_str)
    return {
        "pcr": round(pcr, 3),
        "violation": violation,
        "equilibriumStrike": eq_strike,
        "crossed0_41_0_80": crossed,
        "strikes": [
            {"strike": s, "callOI": rows[s]["callOI"], "putOI": rows[s]["putOI"]}
            for s in strikes
        ],
    }


def main():
    today = date.today()
    today_str = today.isoformat()
    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page.goto("https://web.sensibull.com/option-chain?tradingsymbol=NIFTY",
                   timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(1500)

        expiries = get_expiries(page, today)
        if not expiries:
            print("Could not read expiry list", file=sys.stderr)
            sys.exit(1)
        weekly_expiry, monthly_expiry = pick_weekly_and_monthly(expiries)

        weekly_spot, weekly_rows = fetch_chain_for_expiry(page, weekly_expiry)
        if monthly_expiry == weekly_expiry:
            monthly_spot, monthly_rows = weekly_spot, weekly_rows
        else:
            monthly_spot, monthly_rows = fetch_chain_for_expiry(page, monthly_expiry)

        browser.close()

    weekly = build_timeframe_result(
        weekly_rows, weekly_spot, WEEKLY_STEP, WEEKLY_BAND, state, "weekly", today_str)
    monthly = build_timeframe_result(
        monthly_rows, monthly_spot, MONTHLY_STEP, MONTHLY_BAND, state, "monthly", today_str)

    if weekly is None or monthly is None:
        print(f"Missing data: weekly={weekly is not None} monthly={monthly is not None}",
              file=sys.stderr)
        sys.exit(1)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "spot": weekly_spot,
        "weekly": {"expiry": weekly_expiry.isoformat(), **weekly},
        "monthly": {"expiry": monthly_expiry.isoformat(), **monthly},
    }

    with open(DATA_PATH, "w") as f:
        json.dump(out, f, indent=2)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Wrote pcr_data.json: weekly PCR={weekly['pcr']} monthly PCR={monthly['pcr']}")


if __name__ == "__main__":
    main()
