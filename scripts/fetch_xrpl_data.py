import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ISSUER = "rUNaS5sqRuxZz6V7rBGhoSaZiVYA3ut4UL"
CURRENCY_HEX = "4555524356000000000000000000000000000000"
XRPL_URL = "https://xrplcluster.com/"
RIPPLE_EPOCH = 946684800  # seconds between Unix epoch (1970) and Ripple epoch (2000)


# ── XRPL helpers ─────────────────────────────────────────────────────────────

def xrpl_post(method, params):
    payload = {"method": method, "params": [params]}
    resp = requests.post(XRPL_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", {})
    if result.get("status") == "error":
        raise RuntimeError(f"XRPL error: {result.get('error_message', result.get('error', '?'))}")
    return result


def ripple_to_date(ripple_ts):
    unix_ts = int(ripple_ts) + RIPPLE_EPOCH
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def is_eurcv(amount):
    """True if this Amount dict is our EURCV currency from this issuer."""
    if not isinstance(amount, dict):
        return False
    return (
        amount.get("currency", "").upper() == CURRENCY_HEX.upper()
        and amount.get("issuer") == ISSUER
    )


# ── Fetch all issuer transactions ─────────────────────────────────────────────

def get_all_issuer_transactions():
    all_txs = []
    marker = None
    page = 1

    while True:
        params = {
            "account": ISSUER,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": 400,
            "forward": True,   # oldest first
        }
        if marker:
            params["marker"] = marker

        result = xrpl_post("account_tx", params)
        txs = result.get("transactions", [])
        all_txs.extend(txs)
        print(f"  Page {page}: {len(txs)} txs (total: {len(all_txs)})")

        marker = result.get("marker")
        if not marker:
            break

        page += 1
        time.sleep(0.15)

    return all_txs


# ── Process transactions → supply + holders ───────────────────────────────────

def process_transactions(txs):
    delta_by_date = defaultdict(float)
    holder_first_seen = {}   # address → date of first receipt

    for entry in txs:
        # rippled returns tx inside "tx" or "tx_json" depending on version
        tx = entry.get("tx") or entry.get("tx_json") or {}
        meta = entry.get("meta") or entry.get("metadata") or {}

        if tx.get("TransactionType") != "Payment":
            continue
        if meta.get("TransactionResult") != "tesSUCCESS":
            continue

        date = ripple_to_date(tx.get("date", 0))
        sender = tx.get("Account", "")
        dest = tx.get("Destination", "")

        # Use meta.delivered_amount for accuracy (handles partial payments)
        delivered = meta.get("delivered_amount") or tx.get("Amount", {})

        if not is_eurcv(delivered):
            continue

        value = float(delivered.get("value", 0))

        if sender == ISSUER:
            # Issuance: issuer → holder
            delta_by_date[date] += value
            if dest and dest not in holder_first_seen:
                holder_first_seen[dest] = date

        elif dest == ISSUER:
            # Redemption: holder → issuer
            delta_by_date[date] -= value

    # Cumulative supply
    supply_history = []
    cumulative = 0.0
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(max(0.0, cumulative), 6)})

    # Cumulative holders (first-seen per address)
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
        result.append({"date": date, "marketcap": round(item["supply"] * last_rate, 2)})
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("Récupération des transactions de l'issuer XRPL (sans clé)...")
    txs = get_all_issuer_transactions()
    print(f"Total: {len(txs)} transactions")

    print("Reconstruction supply + holders...")
    supply_history, holders_history = process_transactions(txs)
    print(f"  {len(supply_history)} jours avec activité supply")
    print(f"  {len(holders_history)} jours avec activité holders")

    if not supply_history:
        print("Aucune transaction EURCV trouvée.")
        return

    start_date = supply_history[0]["date"]
    print(f"Récupération des taux EUR/USD depuis le {start_date} (frankfurter.app)...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)

    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    with open("data/xrpl_marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    with open("data/xrpl_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, {len(holders_history)} points holders")


if __name__ == "__main__":
    main()
