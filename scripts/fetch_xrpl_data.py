import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ISSUER = "rUNaS5sqRuxZz6V7rBGhoSaZiVYA3ut4UL"
CURRENCY_HEX = "4555524356000000000000000000000000000000"
XRPL_URL = "https://xrplcluster.com/"
RIPPLE_EPOCH = 946684800

STATE_FILE = "data/xrpl_state.json"
LOOKBACK = 3  # safety margin in days


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── XRPL helpers ───────────────────────────────────────────────────────────────

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
    return (amount.get("currency", "").upper() == CURRENCY_HEX.upper()
            and amount.get("issuer") == ISSUER)


# ── Current supply + holders (authoritative) ──────────────────────────────────

def get_circulating_supply_and_holders():
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


# ── Transaction fetching ───────────────────────────────────────────────────────

def get_issuer_transactions(ledger_index_min=-1):
    """
    Fetch issuer transactions.
    ledger_index_min: -1 = all history; else = specific ledger (inclusive, for incremental).
    """
    all_txs = []
    marker = None
    page = 1
    while True:
        params = {
            "account": ISSUER,
            "ledger_index_min": ledger_index_min,
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


# ── Transaction processing ─────────────────────────────────────────────────────

def process_transactions(txs, initial_cumulative=0.0, initial_holder_first_seen=None):
    """
    Process transactions and return (delta_by_date, holder_first_seen, last_ledger).
    initial_holder_first_seen: existing {addr: date} dict to extend (not overwrite).
    """
    delta_by_date = defaultdict(float)
    holder_first_seen = dict(initial_holder_first_seen) if initial_holder_first_seen else {}
    last_ledger = 0

    for entry in txs:
        tx = entry.get("tx") or entry.get("tx_json") or {}
        meta = entry.get("meta") or entry.get("metadata") or {}

        if tx.get("TransactionType") != "Payment":
            continue
        if meta.get("TransactionResult") != "tesSUCCESS":
            continue

        ledger_index = tx.get("ledger_index") or entry.get("ledger_index", 0)
        if ledger_index and ledger_index > last_ledger:
            last_ledger = ledger_index

        date = ripple_to_date(tx.get("date", 0))
        sender = tx.get("Account", "")
        dest = tx.get("Destination", "")
        delivered = meta.get("delivered_amount") or tx.get("Amount", {})

        if not is_eurcv(delivered):
            continue

        value = float(delivered.get("value", 0))
        if sender == ISSUER:
            delta_by_date[date] += value
            if dest and dest not in holder_first_seen:
                holder_first_seen[dest] = date
        elif dest == ISSUER:
            delta_by_date[date] -= value

    return delta_by_date, holder_first_seen, last_ledger


def build_supply_history(delta_by_date, initial_cumulative=0.0):
    supply_history = []
    cumulative = initial_cumulative
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(max(0.0, cumulative), 6)})
    return supply_history, cumulative


def build_holders_history(holder_first_seen):
    events_by_date = defaultdict(int)
    for date in holder_first_seen.values():
        events_by_date[date] += 1
    holders_history = []
    count = 0
    for date in sorted(events_by_date.keys()):
        count += events_by_date[date]
        holders_history.append({"date": date, "holders": count})
    return holders_history


# ── EUR/USD + marketcap ────────────────────────────────────────────────────────

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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("Récupération de la circulating supply via account_lines...")
    current_supply, trust_line_accounts = get_circulating_supply_and_holders()
    print(f"  Circulating supply actuelle : {current_supply:,.2f} EURCV")
    print(f"  Trust lines EURCV trouvées  : {len(trust_line_accounts)}")

    state = load_json(STATE_FILE, default={})
    existing_marketcap = load_json("data/xrpl_marketcap.json", default=[])
    existing_holders = load_json("data/xrpl_holders.json", default=[])

    if state and state.get("last_ledger") and existing_marketcap:
        # ── Incremental mode ───────────────────────────────────────────────────
        last_ledger = int(state["last_ledger"])
        supply_cumulative = float(state.get("supply_cumulative", 0.0))
        saved_holder_first_seen = state.get("holder_first_seen", {})

        print(f"Mode incrémental — txs depuis ledger {last_ledger} "
              f"(supply base: {supply_cumulative:,.2f})")

        # Fetch with ledger_index_min = last_ledger (inclusive overlap for safety)
        new_txs = get_issuer_transactions(ledger_index_min=last_ledger)
        print(f"  {len(new_txs)} transactions récupérées")

        if not new_txs:
            print("Aucune nouvelle transaction — fichiers inchangés.")
            return

        # Determine the cumulative just before the first new date
        # Since we re-fetch from last_ledger (inclusive), some txs overlap with saved state.
        # We detect truly new txs by ledger_index > last_ledger, apply deltas on top of
        # supply_cumulative which already includes everything up to last_ledger.
        truly_new_txs = []
        for entry in new_txs:
            tx = entry.get("tx") or entry.get("tx_json") or {}
            li = tx.get("ledger_index") or entry.get("ledger_index", 0)
            if li > last_ledger:
                truly_new_txs.append(entry)

        print(f"  {len(truly_new_txs)} vraiment nouvelles txs (ledger > {last_ledger})")

        if not truly_new_txs:
            print("Aucune nouvelle transaction au-delà du dernier ledger — fichiers inchangés.")
            return

        delta_by_date, updated_holder_first_seen, new_last_ledger = process_transactions(
            truly_new_txs,
            initial_holder_first_seen=saved_holder_first_seen,
        )

        new_supply_history, new_cumulative = build_supply_history(delta_by_date, supply_cumulative)
        print(f"  {len(new_supply_history)} nouveaux jours avec activité supply")

        # Find the first new date to determine merge boundary
        if new_supply_history:
            first_new_date = new_supply_history[0]["date"]
        else:
            first_new_date = today

        # Merge: keep existing before first_new_date, append new
        kept_marketcap = [pt for pt in existing_marketcap if pt["date"] < first_new_date]
        merged_raw_supply = (
            [{"date": pt["date"], "supply": pt["supply"]} for pt in kept_marketcap]
            + new_supply_history
        )

        # Rebuild holders history from updated cache
        new_holders_history = build_holders_history(updated_holder_first_seen)
        # Merge holders: keep before first new holder date, append new
        first_new_holder_date = new_holders_history[0]["date"] if new_holders_history else today
        kept_holders = [pt for pt in existing_holders if pt["date"] < first_new_holder_date]
        merged_holders = kept_holders + new_holders_history

        new_state = {
            "last_ledger": new_last_ledger if new_last_ledger > last_ledger else last_ledger,
            "supply_cumulative": new_cumulative,
            "holder_first_seen": updated_holder_first_seen,
        }
        current_holders = len(trust_line_accounts & set(updated_holder_first_seen.keys()))

    else:
        # ── Full fetch (first run) ─────────────────────────────────────────────
        print("Premier fetch complet...")
        txs = get_issuer_transactions(ledger_index_min=-1)
        print(f"Total: {len(txs)} transactions")

        print("Reconstruction historique supply + holders...")
        delta_by_date, holder_first_seen, new_last_ledger = process_transactions(txs)
        merged_raw_supply, new_cumulative = build_supply_history(delta_by_date)
        merged_holders = build_holders_history(holder_first_seen)

        current_holders = len(trust_line_accounts & set(holder_first_seen.keys()))
        print(f"  Holders actifs : {current_holders}")

        new_state = {
            "last_ledger": new_last_ledger,
            "supply_cumulative": new_cumulative,
            "holder_first_seen": holder_first_seen,
        }
        updated_holder_first_seen = holder_first_seen

    # ── Override today with authoritative account_lines value ──────────────────
    if merged_raw_supply and merged_raw_supply[-1]["date"] == today:
        merged_raw_supply[-1]["supply"] = current_supply
    else:
        merged_raw_supply.append({"date": today, "supply": current_supply})

    if merged_holders and merged_holders[-1]["date"] == today:
        merged_holders[-1]["holders"] = current_holders
    else:
        merged_holders.append({"date": today, "holders": current_holders})

    if not merged_raw_supply:
        print("Aucune transaction EURCV trouvée.")
        return

    # ── EUR/USD + marketcap ────────────────────────────────────────────────────
    start_date = merged_raw_supply[0]["date"]
    from datetime import timedelta as _td
    rates_start = (
        datetime.strptime(start_date, "%Y-%m-%d") - _td(days=7)
    ).strftime("%Y-%m-%d")
    print(f"Récupération des taux EUR/USD depuis le {rates_start}...")
    eur_usd_rates = fetch_eur_usd_rates(rates_start)

    if eur_usd_rates:
        seed_rate = eur_usd_rates[max(eur_usd_rates.keys())]
        for item in merged_raw_supply:
            if item["date"] not in eur_usd_rates:
                eur_usd_rates[item["date"]] = seed_rate

    marketcap_history = compute_marketcap(merged_raw_supply, eur_usd_rates)

    # ── Save ──────────────────────────────────────────────────────────────────
    save_json(STATE_FILE, new_state)
    save_json("data/xrpl_marketcap.json", marketcap_history)
    save_json("data/xrpl_holders.json", merged_holders)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, "
          f"{len(merged_holders)} points holders")
    print(f"  État sauvegardé : ledger {new_state['last_ledger']}, "
          f"supply_cumulative={new_state['supply_cumulative']:,.2f}")


if __name__ == "__main__":
    main()
