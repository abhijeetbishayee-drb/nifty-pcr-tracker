import json
import os
import re
import sys
from datetime import datetime, date, timezone

from playwright.sync_api import sync_playwright

from pcr_common import (
    SYMBOLS, BAND, VIOLATION_LOW, VIOLATION_HIGH, CROSS_LOW, CROSS_HIGH,
    select_band, compute_pcr, equilibrium_strike,
)

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_state.json")
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_data.json")

# Wide-viewport combined layout renders one row as:
# call_chg_pct, call_oi(lakh), call_ltp, strike, iv, put_ltp, put_oi(lakh), put_chg_pct
ROW_RE = re.compile(
    r"(-?[\d.]+%)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(\d+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(-?[\d.]+%)"
)


def month_key(d):
    return (d.year, d.month)


def parse_expiry_label(label, today):
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


def fetch_chain_for_expiry(page, tradingsymbol, expiry_date):
    url = f"https://web.sensibull.com/option-chain?tradingsymbol={tradingsymbol}&expiry={expiry_date.isoformat()}"
    page.goto(url, timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")

    spot_match = re.search(re.escape(tradingsymbol) + r"\s*\n?\s*([\d,]+\.\d+)", text)
    spot = float(spot_match.group(1).replace(",", "")) if spot_match else None

    rows = {}
    for m in ROW_RE.finditer(text):
        (_call_chg_pct, call_oi, call_ltp, strike, _iv,
         put_ltp, put_oi, _put_chg_pct) = m.groups()
        rows[int(strike)] = {
            "callOI": float(call_oi), "putOI": float(put_oi),
            "callLTP": float(call_ltp), "putLTP": float(put_ltp),
        }
    return spot, rows


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
        "bandStrikes": len(strikes),
        "bandWanted": 2 * band + 1,
        "violation": violation,
        "equilibriumStrike": eq_strike,
        "crossed0_41_0_80": crossed,
        "strikes": [
            {
                "strike": s, "callOI": rows[s]["callOI"], "putOI": rows[s]["putOI"],
                "callLTP": rows[s]["callLTP"], "putLTP": rows[s]["putLTP"],
            }
            for s in strikes
        ],
    }


def fetch_symbol(page, symbol_key, cfg, today, today_str, state):
    tradingsymbol = cfg["tradingsymbol"]
    page.goto(f"https://web.sensibull.com/option-chain?tradingsymbol={tradingsymbol}",
              timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(1500)

    expiries = get_expiries(page, today)
    if not expiries:
        print(f"{tradingsymbol}: could not read expiry list", file=sys.stderr)
        return None
    weekly_expiry, monthly_expiry = pick_weekly_and_monthly(expiries)
    has_weekly = cfg.get("hasWeekly", True)

    if has_weekly:
        weekly_spot, weekly_rows = fetch_chain_for_expiry(page, tradingsymbol, weekly_expiry)
        if monthly_expiry == weekly_expiry:
            monthly_spot, monthly_rows = weekly_spot, weekly_rows
        else:
            monthly_spot, monthly_rows = fetch_chain_for_expiry(page, tradingsymbol, monthly_expiry)
    else:
        # No separate weekly for this symbol (e.g. BANKNIFTY, monthly-only since 2023) --
        # fetch the monthly chain only.
        monthly_spot, monthly_rows = fetch_chain_for_expiry(page, tradingsymbol, monthly_expiry)

    monthly = build_timeframe_result(
        monthly_rows, monthly_spot, cfg["monthlyStep"], BAND, state, f"{symbol_key}_monthly", today_str)
    if monthly is None:
        print(f"{tradingsymbol}: missing monthly data", file=sys.stderr)
        return None

    result = {
        "spot": monthly_spot,
        "monthly": {"expiry": monthly_expiry.isoformat(), **monthly},
    }

    if has_weekly:
        weekly = build_timeframe_result(
            weekly_rows, weekly_spot, cfg["weeklyStep"], BAND, state, f"{symbol_key}_weekly", today_str)
        if weekly is None:
            print(f"{tradingsymbol}: missing weekly data", file=sys.stderr)
            return None
        result["spot"] = weekly_spot
        result["weekly"] = {"expiry": weekly_expiry.isoformat(), **weekly}

    return result


def main():
    today = date.today()
    today_str = today.isoformat()
    state = load_state()

    out = {"generatedAt": datetime.now(timezone.utc).isoformat()}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        for symbol_key, cfg in SYMBOLS.items():
            result = fetch_symbol(page, symbol_key, cfg, today, today_str, state)
            if result is not None:
                out[symbol_key] = result
        browser.close()

    if "nifty" not in out or "bankNifty" not in out:
        print("Missing one or both symbols, aborting write to avoid a partial snapshot",
              file=sys.stderr)
        sys.exit(1)

    with open(DATA_PATH, "w") as f:
        json.dump(out, f, indent=2)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Wrote pcr_data.json: "
          f"nifty weekly={out['nifty']['weekly']['pcr']} monthly={out['nifty']['monthly']['pcr']}, "
          f"bankNifty monthly={out['bankNifty']['monthly']['pcr']}")


if __name__ == "__main__":
    main()
