import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ISSUER     = "GCEYGIVOLAVBF2TG2RUSGTUJCIN75KEX3NGLMY4VPL4GFE5L355AXW3G"
ASSET_CODE = "EURCV"
HORIZON    = "https://horizon.stellar.org"
EXPERT     = f"https://api.stellar.expert/explorer/public/asset/{ASSET_CODE}-{ISSUER}"


def iso_to_date(iso):
    return iso[:10]


# ── Current state ─────────────────────────────────────────────────────────────

def get_current_supply():
    """Horizon /assets — authoritative circulating supply."""
    resp = requests.get(
        f"{HORIZON}/assets",
        params={"asset_code": ASSET_CODE, "asset_issuer": ISSUER},
        timeout=30,
    )
    resp.raise_for_status()
    records = resp.json().get("_embedded", {}).get("records", [])
    if not records:
        raise RuntimeError(f"Asset {ASSET_CODE} not found on Stellar Horizon")
    return float(records[0].get("amount", 0))


def get_current_holders():
    """stellar.expert /holders — accounts with balance > 0."""
    resp = requests.get(f"{EXPERT}/holders", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Try top-level total_count first, fall back to counting embedded records
    if "total_count" in data:
        return int(data["total_count"])
    records = data.get("_embedded", {}).get("records", [])
    return len([r for r in records if float(r.get("balance", 0)) > 0])


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

    print("Récupération supply actuelle via Horizon assets...")
    current_supply = get_current_supply()
    print(f"  Circulating supply : {current_supply:,.7f} EURCV")

    print("Récupération holders actuels via stellar.expert...")
    current_holders = get_current_holders()
    print(f"  Holders actifs     : {current_holders}")

    print("Récupération des opérations issuer Stellar (Horizon)...")
    ops = get_all_issuer_operations()
    print(f"Total: {len(ops)} opérations")

    print("Reconstruction historique supply + holders...")
    supply_history, holders_history = process_operations(ops)
    print(f"  {len(supply_history)} jours avec activité supply")
    print(f"  {len(holders_history)} jours avec activité holders")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Override last point with authoritative current values
    if supply_history and supply_history[-1]["date"] == today:
        supply_history[-1]["supply"] = current_supply
    else:
        supply_history.append({"date": today, "supply": current_supply})

    if holders_history and holders_history[-1]["date"] == today:
        holders_history[-1]["holders"] = current_holders
    else:
        holders_history.append({"date": today, "holders": current_holders})

    if not supply_history:
        print("Aucune opération EURCV trouvée.")
        return

    start_date = supply_history[0]["date"]
    print(f"Récupération taux EUR/USD depuis le {start_date}...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)

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
