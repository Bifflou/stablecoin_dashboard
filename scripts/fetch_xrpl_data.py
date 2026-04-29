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
    if not isinstance(amount, dict):
        return False
    return (
        amount.get("currency", "").upper() == CURRENCY_HEX.upper()
        and amount.get("issuer") == ISSUER
    )


# ── Current circulating supply + holders via account_lines ────────────────────
#
# From the issuer's perspective, balance in a trust line is negative when
# the issuer owes tokens to the holder (i.e., the holder holds tokens).
# Circulating supply = sum(abs(balance)) for all EURCV lines where balance < 0.
# Holders = count of those lines.

def get_circulating_supply_and_holders():
    """Returns (circulating_supply, set_of_accounts_with_eurcv_trust_lines)."""
    total_supply = 0.0
    trust_line_accounts = set()
    marker = None
    page = 1

    while True:
        params = {"account": ISSUER, "limit": 400}
        if marker:
            params["marker"] = marker

        result = xrpl_post("account_lines", params)
        for line in result.get("lines", []):
            if line.get("currency", "").upper() != CURRENCY_HEX.upper():
                continue
            balance = float(line.get("balance", 0))
            trust_line_accounts.add(line["account"])
            if balance < 0:
                total_supply += abs(balance)

        marker = result.get("marker")
        if not marker:
            break

        print(f"  account_lines page {page}: {len(trust_line_accounts)} trust lines so far")
        page += 1
        time.sleep(0.15)

    return round(total_supply, 6), trust_line_accounts


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
            "forward": True,
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


# ── Process transactions → historical shape ───────────────────────────────────
#
# Transaction replay gives us WHEN supply changed and by how much.
# We use it only for the historical shape of the curve; the current endpoint
# is always overridden by the authoritative account_lines value.

def process_transactions(txs):
    delta_by_date = defaultdict(float)
    holder_first_seen = {}  # address → date of first receipt

    for entry in txs:
        tx = entry.get("tx") or entry.get("tx_json") or {}
        meta = entry.get("meta") or entry.get("metadata") or {}

        if tx.get("TransactionType") != "Payment":
            continue
        if meta.get("TransactionResult") != "tesSUCCESS":
            continue

        date = ripple_to_date(tx.get("date", 0))
        sender = tx.get("Account", "")
        dest = tx.get("Destination", "")

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

    # Cumulative supply (historical shape only; last point will be overridden)
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

    return supply_history, holders_history, set(holder_first_seen.keys())


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
        result.append({"date": date, "marketcap": round(item["supply"] * last_rate, 2), "supply": round(item["supply"], 2)})
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    # 1. Authoritative current state via account_lines
    print("Récupération de la circulating supply via account_lines...")
    current_supply, trust_line_accounts = get_circulating_supply_and_holders()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"  Circulating supply actuelle : {current_supply:,.2f} EURCV")
    print(f"  Trust lines EURCV trouvées  : {len(trust_line_accounts)}")

    # 2. Historical shape via transaction replay
    print("Récupération des transactions de l'issuer XRPL...")
    txs = get_all_issuer_transactions()
    print(f"Total: {len(txs)} transactions")

    print("Reconstruction historique supply + holders...")
    supply_history, holders_history, ever_received = process_transactions(txs)
    print(f"  {len(supply_history)} jours avec activité supply (replay)")
    print(f"  {len(holders_history)} jours avec activité holders (replay)")

    # 3. Override/append today with account_lines values (authoritative endpoint)
    # Holders = comptes qui ont réellement reçu des EURCV ET ont encore une trust line active
    current_holders = len(trust_line_accounts & ever_received)
    print(f"  Holders actifs (trust line ∩ ever received) : {current_holders}")

    if supply_history and supply_history[-1]["date"] == today:
        supply_history[-1]["supply"] = current_supply
    else:
        supply_history.append({"date": today, "supply": current_supply})

    if holders_history and holders_history[-1]["date"] == today:
        holders_history[-1]["holders"] = current_holders
    else:
        holders_history.append({"date": today, "holders": current_holders})

    if not supply_history:
        print("Aucune transaction EURCV trouvée.")
        return

    start_date = supply_history[0]["date"]
    print(f"Récupération des taux EUR/USD depuis le {start_date}...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)

    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    with open("data/xrpl_marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    with open("data/xrpl_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, {len(holders_history)} points holders")
    if marketcap_history:
        print(f"  Dernier point : {marketcap_history[-1]}")


if __name__ == "__main__":
    main()
