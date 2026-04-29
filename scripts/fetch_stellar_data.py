import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ISSUER       = "GCEYGIVOLAVBF2TG2RUSGTUJCIN75KEX3NGLMY4VPL4GFE5L355AXW3G"
ASSET_CODE   = "EURCV"
HORIZON      = "https://horizon.stellar.org"
EXPERT_BASE  = "https://api.stellar.expert"
EXPERT       = f"{EXPERT_BASE}/explorer/public/asset/{ASSET_CODE}-{ISSUER}"
STELLAR_SCALE = 10 ** 7  # stellar.expert balances are in raw units (7 decimal places)


def iso_to_date(iso):
    return iso[:10]


# ── Current state ─────────────────────────────────────────────────────────────
#
# Horizon /assets.amount is unreliable for this asset (returns 0 despite active
# holders). We use stellar.expert /holders instead: paginate all records, sum
# balances > 0 for circulating supply, count them for holders.

def get_circulating_supply_and_holders():
    """stellar.expert /holders — sum(balance) = circulating supply, count = holders."""
    total_supply = 0.0
    holder_count = 0
    url = f"{EXPERT}/holders"
    first = True
    page = 1

    while True:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])

        if first and records:
            print(f"  (sample balance raw: {records[0].get('balance')})")
            first = False

        for r in records:
            bal = float(r.get("balance", 0)) / STELLAR_SCALE
            if bal > 0:
                total_supply += bal
                holder_count += 1

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break

        # next_href from stellar.expert is a relative path
        if next_href.startswith("/"):
            next_href = EXPERT_BASE + next_href
        url = next_href
        page += 1
        time.sleep(0.1)

    return round(total_supply, 7), holder_count


# ── Historical operations ─────────────────────────────────────────────────────

def get_all_issuer_operations():
    """Paginate all operations on the issuer account, oldest first."""
    all_ops = []
    url = f"{HORIZON}/accounts/{ISSUER}/operations"
    params = {"order": "asc", "limit": 200}
    use_params = True
    page = 1

    while True:
        resp = requests.get(url, params=(params if use_params else None), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])
        all_ops.extend(records)
        print(f"  Page {page}: {len(records)} ops (total: {len(all_ops)})")

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break

        url = next_href
        use_params = False
        page += 1
        time.sleep(0.15)

    return all_ops


def process_operations(ops):
    """
    Reconstruct daily supply delta and holder first-seen from operations.

    Minting  : payment from ISSUER → holder  (supply +)
    Burning  : payment from holder → ISSUER  (supply -)
    Clawback : issuer claws back from holder (supply -)
    """
    delta_by_date = defaultdict(float)
    holder_first_seen = {}

    for op in ops:
        op_type = op.get("type", "")
        date = iso_to_date(op.get("created_at", "") or "")
        if not date:
            continue

        if op_type == "payment":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            amount = float(op.get("amount", 0))
            src = op.get("from", "")
            dst = op.get("to", "")

            if src == ISSUER:
                delta_by_date[date] += amount
                if dst and dst != ISSUER and dst not in holder_first_seen:
                    holder_first_seen[dst] = date
            elif dst == ISSUER:
                delta_by_date[date] -= amount

        elif op_type == "clawback":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            delta_by_date[date] -= float(op.get("amount", 0))

    # Cumulative supply
    supply_history = []
    cumulative = 0.0
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(max(0.0, cumulative), 7)})

    # Holders history (first-seen per address)
    events_by_date = defaultdict(int)
    for date in holder_first_seen.values():
        events_by_date[date] += 1

    holders_history = []
    count = 0
    for date in sorted(events_by_date.keys()):
        count += events_by_date[date]
        holders_history.append({"date": date, "holders": count})

    return supply_history, holders_history


# ── EUR/USD + market cap ──────────────────────────────────────────────────────

def fetch_eur_usd_rates(start_date):
    url = f"https://api.frankfurter.app/{start_date}.."
    resp = requests.get(url, params={"from": "EUR", "to": "USD"}, timeout=30)
    resp.raise_for_status()
    return {d: r["USD"] for d, r in resp.json().get("rates", {}).items()}


def compute_marketcap(supply_history, eur_usd_rates):
    result = []
    last_rate = None
    for item in supply_history:
        date = item["date"]
        if date in eur_usd_rates:
            last_rate = eur_usd_rates[date]
        if last_rate is None:
            continue
        result.append({
            "date": date,
            "marketcap": round(item["supply"] * last_rate, 2),
            "supply": round(item["supply"], 2),
        })
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("Récupération supply + holders via stellar.expert /holders...")
    current_supply, current_holders = get_circulating_supply_and_holders()
    print(f"  Circulating supply : {current_supply:,.7f} EURCV")
    print(f"  Holders actifs     : {current_holders}")

    print("Récupération des opérations issuer Stellar (Horizon, pour holders)...")
    ops = get_all_issuer_operations()
    print(f"Total: {len(ops)} opérations")

    # Supply history: operation replay on Stellar is unreliable (the 15 M EURCV
    # were not issued via simple payments visible on the issuer account).
    # We output today's authoritative value only — no false historical zeros.
    _, holders_history = process_operations(ops)
    print(f"  {len(holders_history)} jours avec activité holders")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Supply: single authoritative point (today)
    supply_history = [{"date": today, "supply": current_supply}]

    # Holders: append/override today with authoritative count
    if holders_history and holders_history[-1]["date"] == today:
        holders_history[-1]["holders"] = current_holders
    else:
        holders_history.append({"date": today, "holders": current_holders})

    if not supply_history:
        print("Aucune opération EURCV trouvée.")
        return

    from datetime import timedelta
    start_date = supply_history[0]["date"]
    # Fetch rates starting 7 days before to always have a rate to fall back on
    rates_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Récupération taux EUR/USD depuis le {rates_start}...")
    eur_usd_rates = fetch_eur_usd_rates(rates_start)

    # Pre-seed last_rate with the most recent available rate so forward-fill
    # works even when supply_history contains only today (rate may lag by a day)
    if eur_usd_rates:
        seed_rate = eur_usd_rates[max(eur_usd_rates.keys())]
        for item in supply_history:
            if item["date"] not in eur_usd_rates:
                eur_usd_rates[item["date"]] = seed_rate

    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    with open("data/stellar_marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    with open("data/stellar_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, {len(holders_history)} points holders")
    if marketcap_history:
        print(f"  Dernier point : {marketcap_history[-1]}")


if __name__ == "__main__":
    main()
