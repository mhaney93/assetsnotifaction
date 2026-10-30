"""
Watches for a change in "who's #1" across two asset classes:
  - largest cryptocurrency (by market cap)
  - largest precious metal (by total mined value: spot price x cumulative
    mined supply, since metals don't have a market cap in the usual sense)

Sends an ntfy.sh push notification only when the #1 spot changes hands
compared to the last run. State is persisted to state.json next to this
script, so runs can be scheduled (e.g. via Windows Task Scheduler) without
spamming a notification every time -- only on an actual change of leader.
"""

import json
import sys
import time
from pathlib import Path

import requests
import yfinance as yf

NTFY_TOPIC = "mh-marketcap-8f3k2"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_PATH = Path(__file__).parent / "state.json"

# Cumulative mined supply, in metric tonnes (rough public estimates).
# These change slowly -- update occasionally, no need for a live feed.
METAL_SUPPLY_TONNES = {
    "Gold": 212_582,
    "Silver": 1_740_000,
    "Platinum": 9_000,
    "Palladium": 8_000,
}
TROY_OZ_PER_TONNE = 32_150.7

# Futures tickers used as a spot-price proxy, USD per troy ounce.
METAL_FUTURES_TICKERS = {
    "Gold": "GC=F",
    "Silver": "SI=F",
    "Platinum": "PL=F",
    "Palladium": "PA=F",
}


HTTP_HEADERS = {"User-Agent": "assetsnotifaction/1.0 (+https://github.com/mhaney93/assetsnotifaction)"}


def _top_crypto_coinpaprika():
    resp = requests.get(
        "https://api.coinpaprika.com/v1/tickers",
        params={"limit": 5},
        headers=HTTP_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = [c for c in resp.json() if c.get("rank")]
    if not data:
        raise RuntimeError(f"empty response body: {resp.text[:200]!r}")
    top = min(data, key=lambda c: c["rank"])
    return top["symbol"].upper(), top["quotes"]["USD"]["market_cap"]


def _top_crypto_coingecko():
    resp = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 1,
            "page": 1,
        },
        headers=HTTP_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise RuntimeError(f"empty response body: {resp.text[:200]!r}")
    return data[0]["symbol"].upper(), data[0]["market_cap"]


CRYPTO_SOURCES = [
    ("coinpaprika", _top_crypto_coinpaprika),
    ("coingecko", _top_crypto_coingecko),
]


def get_top_crypto(attempts=3):
    last_err = None
    for attempt in range(attempts):
        for source, fetch in CRYPTO_SOURCES:
            try:
                leader, value = fetch()
                return {"class": "crypto", "leader": leader, "value_usd": value}
            except Exception as e:
                last_err = f"{source}: {e}"
                print(f"  warn: crypto fetch attempt {attempt + 1}/{attempts} failed: {last_err}", file=sys.stderr)
        if attempt < attempts - 1:
            time.sleep(10 * (attempt + 1))
    print(f"  warn: giving up on crypto after {attempts} attempts: {last_err}", file=sys.stderr)
    return {"class": "crypto", "leader": None, "value_usd": None}


# A single-ticker price fetch that lands far outside this range is almost
# certainly a bad/stale quote from the free yfinance endpoint, not a real
# market move -- retry rather than let it flip the leader for one run.
METAL_PLAUSIBLE_PRICE_USD = {
    "Gold": (1000, 50000),
    "Silver": (10, 200),
    "Platinum": (300, 3000),
    "Palladium": (300, 5000),
}


def _fetch_metal_price(name, ticker, attempts=3):
    lo, hi = METAL_PLAUSIBLE_PRICE_USD[name]
    last_err = None
    for attempt in range(attempts):
        try:
            price = yf.Ticker(ticker).fast_info["last_price"]
            if lo <= price <= hi:
                return price
            last_err = f"implausible price {price} (expected {lo}-{hi})"
        except Exception as e:
            last_err = str(e)
        print(f"  warn: {ticker} fetch attempt {attempt + 1}/{attempts} failed: {last_err}", file=sys.stderr)
        if attempt < attempts - 1:
            time.sleep(5)
    print(f"  warn: giving up on {ticker} after {attempts} attempts: {last_err}", file=sys.stderr)
    return None


def get_top_metal():
    best_name, best_value = None, -1
    for name, ticker in METAL_FUTURES_TICKERS.items():
        price = _fetch_metal_price(name, ticker)
        if price is None:
            continue
        supply_oz = METAL_SUPPLY_TONNES[name] * TROY_OZ_PER_TONNE
        value = price * supply_oz
        if value > best_value:
            best_value = value
            best_name = name
    return {"class": "metal", "leader": best_name, "value_usd": best_value}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def notify(title, message):
    requests.post(
        NTFY_URL,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "rotating_light"},
        timeout=15,
    )


def main():
    results = [get_top_crypto(), get_top_metal()]
    old_state = load_state()
    new_state = dict(old_state)

    for r in results:
        cls, leader = r["class"], r["leader"]
        old_leader = old_state.get(cls)

        if leader is None:
            # Data source unavailable this run -- keep the previous leader
            # rather than failing the job or wiping state.
            print(f"{cls}: no data this run, keeping previous leader {old_leader}")
            continue

        new_state[cls] = leader
        print(f"{cls}: {leader} (${r['value_usd']:,.0f})")

        if old_leader is None:
            continue  # first run for this class, just establish baseline
        if leader != old_leader:
            notify(
                f"New #1 {cls}!",
                f"{old_leader} has been overthrown by {leader} "
                f"as the top {cls} by market cap.",
            )
            print(f"  -> CHANGE: {old_leader} -> {leader}, notification sent")

    save_state(new_state)


if __name__ == "__main__":
    main()
