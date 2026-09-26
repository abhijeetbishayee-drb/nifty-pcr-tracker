"""Backfill weekly/monthly PCR history from NSE's F&O bhavcopy archive.

Read-only, no broker, no auth. Same archive + fetch pattern as the Rollover
project's backfill.py (nsearchives.nseindia.com is not behind the Akamai bot
wall that blocks nseindia.com itself). Bhavcopy is end-of-day only, so this
covers PAST trading days -- today's live reading still comes from
fetch_pcr.py via Sensibull.
"""
import csv
import io
import ssl
import sys
import time
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import certifi

from fetch_pcr import SYMBOLS, BAND, VIOLATION_LOW, VIOLATION_HIGH, select_band, compute_pcr, equilibrium_strike

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "bhav"
HISTORY_PATH = ROOT / "pcr_history.csv"
BACKFILL_DAYS = 180  # ~6 months of trading days

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"


def fetch_bhavcopy(d: date):
    tag = d.strftime("%Y%m%d")
    cached = CACHE / f"{tag}.csv"
    if cached.exists():
        return list(csv.DictReader(cached.open()))
    req = urllib.request.Request(URL.format(d=tag), headers={
        "User-Agent": UA, "Referer": "https://www.nseindia.com/", "Accept": "*/*"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        raw = urllib.request.urlopen(req, timeout=30, context=ctx).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # holiday / non-trading day
        raise
    if len(raw) < 50_000:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text)
    time.sleep(1.2)  # be polite to NSE
    return list(csv.DictReader(io.StringIO(text)))


def index_options(rows, tradingsymbol):
    return [r for r in rows if r["FinInstrmTp"] == "IDO" and r["TckrSymb"] == tradingsymbol]


def pick_weekly_and_monthly_from_bhav(opt_rows, today):
    expiries = sorted({r["XpryDt"] for r in opt_rows})
    future = [e for e in expiries if e >= today.isoformat()]
    if not future:
        return None, None
    weekly = future[0]
    weekly_month = weekly[:7]  # "YYYY-MM"
    same_month = [e for e in future if e[:7] == weekly_month]
    monthly = max(same_month)
    return weekly, monthly


def rows_for_expiry(opt_rows, expiry):
    out = {}
    for r in opt_rows:
        if r["XpryDt"] != expiry:
            continue
        strike = int(float(r["StrkPric"]))
        oi = float(r["OpnIntrst"] or 0)
        entry = out.setdefault(strike, {"callOI": 0.0, "putOI": 0.0})
        if r["OptnTp"] == "CE":
            entry["callOI"] = oi
        elif r["OptnTp"] == "PE":
            entry["putOI"] = oi
    return out


def compute_timeframe(opt_rows, expiry, spot, step):
    if not expiry:
        return None
    rows = rows_for_expiry(opt_rows, expiry)
    strikes = select_band(rows, spot, step, BAND)
    if not strikes:
        return None
    pcr = compute_pcr(rows, strikes)
    if pcr is None:
        return None
    violation = pcr < VIOLATION_LOW or pcr > VIOLATION_HIGH
    eq_strike = equilibrium_strike(rows, strikes) if violation else None
    return round(pcr, 3), violation, eq_strike


def main():
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else BACKFILL_DAYS
    today = date.today()
    existing_dates = set()
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open() as f:
            existing_dates = {(row["date"], row["symbol"]) for row in csv.DictReader(f)}

    fieldnames = ["date", "symbol", "spot", "weeklyExpiry", "weeklyPCR", "weeklyViolation",
                  "weeklyEqStrike", "monthlyExpiry", "monthlyPCR", "monthlyViolation", "monthlyEqStrike"]
    new_rows = []

    d = today - timedelta(days=1)  # bhavcopy for today isn't out yet during market hours
    checked = 0
    while checked < n_days:
        if d.weekday() < 5:  # Mon-Fri only
            checked += 1
            bhav = fetch_bhavcopy(d)
            if bhav:
                for symbol_key, cfg in SYMBOLS.items():
                    tradingsymbol = cfg["tradingsymbol"]
                    if (d.isoformat(), tradingsymbol) in existing_dates:
                        continue
                    opt_rows = index_options(bhav, tradingsymbol)
                    if not opt_rows:
                        continue
                    weekly_e, monthly_e = pick_weekly_and_monthly_from_bhav(opt_rows, d)
                    spot = float(opt_rows[0]["UndrlygPric"] or 0)
                    weekly = compute_timeframe(opt_rows, weekly_e, spot, cfg["weeklyStep"])
                    monthly = compute_timeframe(opt_rows, monthly_e, spot, cfg["monthlyStep"])
                    if weekly is None or monthly is None:
                        continue
                    new_rows.append({
                        "date": d.isoformat(), "symbol": tradingsymbol, "spot": spot,
                        "weeklyExpiry": weekly_e, "weeklyPCR": weekly[0],
                        "weeklyViolation": weekly[1], "weeklyEqStrike": weekly[2] or "",
                        "monthlyExpiry": monthly_e, "monthlyPCR": monthly[0],
                        "monthlyViolation": monthly[1], "monthlyEqStrike": monthly[2] or "",
                    })
                print(f"  {d.isoformat()}: ok", flush=True)
            else:
                print(f"  {d.isoformat()}: no bhavcopy (holiday?)", flush=True)
        d -= timedelta(days=1)

    if new_rows:
        write_header = not HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)

    print(f"\nDONE: {len(new_rows)} new rows -> {HISTORY_PATH}")


if __name__ == "__main__":
    main()
