import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
TOKEN_ADDRESS = "0x5422374B27757da72d5265cC745ea906E0446634"
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

STATE_FILE = "data/usdcv_eth_state.json"
LOOKBACK = 3  # safety margin in days → ~1000-block overlap


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── Etherscan helpers ──────────────────────────────────────────────────────────

def etherscan_get(params):
    params["chainid"] = CHAIN_ID
    params["apikey"] = ETHERSCAN_API_KEY
    resp = requests.get(ETHERSCAN_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def get_token_decimals():
    data = etherscan_get({
        "module": "proxy", "action": "eth_call",
        "to": TOKEN_ADDRESS, "data": "0x313ce567", "tag": "latest",
    })
    return int(data.get("result", "0x12"), 16)


# ── Log fetching ───────────────────────────────────────────────────────────────

def fetch_transfer_logs(from_block):
    """Fetch Transfer logs starting from from_block (inclusive). Returns raw log list."""
    all_logs = []
    seen = set()
    current_from = from_block

    while True:
        page = 1
        last_block_in_batch = None

        while True:
            data = etherscan_get({
                "module": "logs", "action": "getLogs",
                "address": TOKEN_ADDRESS, "topic0": TRANSFER_TOPIC,
                "fromBlock": current_from, "toBlock": "latest",
                "page": page, "offset": 1000,
            })
            if data["status"] != "1" or not data["result"]:
                return all_logs

            logs = data["result"]
            new = 0
            for log in logs:
                key = (log["transactionHash"], log["logIndex"])
                if key not in seen:
                    seen.add(key)
                    all_logs.append(log)
                    new += 1

            last_block_in_batch = int(logs[-1]["blockNumber"], 16)
            print(f"  Page {page} (from block {current_from}): {new} nouveaux events (total: {len(all_logs)})")

            if len(logs) < 1000:
                return all_logs

            page += 1
            time.sleep(0.25)

            if page > 10:
                current_from = last_block_in_batch
                break

    return all_logs


# ── Apply logs to balances ─────────────────────────────────────────────────────

def build_daily_snapshots(logs, balances, decimals):
    """
    Apply Transfer logs to balances dict (mutated in place) and build daily snapshots.
    Returns (supply_history, holders_history) as lists of dicts.
    """
    events_by_date = defaultdict(list)
    for log in logs:
        ts = int(log["timeStamp"], 16)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        events_by_date[date].append(log)

    supply_history = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        for log in events_by_date[date]:
            from_addr = "0x" + log["topics"][1][-40:]
            to_addr = "0x" + log["topics"][2][-40:]
            amount = int(log["data"], 16)
            if from_addr.lower() != ZERO_ADDRESS:
                balances[from_addr.lower()] = balances.get(from_addr.lower(), 0) - amount
            if to_addr.lower() != ZERO_ADDRESS:
                balances[to_addr.lower()] = balances.get(to_addr.lower(), 0) + amount

        supply_tokens = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        holders = sum(1 for v in balances.values() if v > 0)
        supply_history.append({"date": date, "supply": round(supply_tokens, 2)})
        holders_history.append({"date": date, "holders": holders})

    return supply_history, holders_history


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("USDCV ETH — Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    state = load_json(STATE_FILE, default={})
    existing_supply = load_json("data/usdcv_eth_marketcap.json", default=[])
    existing_holders = load_json("data/usdcv_eth_holders.json", default=[])

    if state and state.get("last_block") and existing_supply:
        # ── Incremental mode ───────────────────────────────────────────────────
        last_block = int(state["last_block"])
        balances = {k: int(v) for k, v in state.get("balances", {}).items()}

        # Safety overlap: go back 1000 blocks (~3 h on Ethereum, covers LOOKBACK days)
        from_block = max(0, last_block - 1000)
        print(f"Mode incrémental — Transfer logs depuis le bloc {from_block} "
              f"(dernier bloc connu: {last_block})")

        fetched_logs = fetch_transfer_logs(from_block)
        print(f"  {len(fetched_logs)} logs récupérés")

        # Split into overlap (already in state) and truly new
        overlap_logs = [l for l in fetched_logs if int(l["blockNumber"], 16) <= last_block]
        truly_new_logs = [l for l in fetched_logs if int(l["blockNumber"], 16) > last_block]
        print(f"  {len(overlap_logs)} de chevauchement (ignorés), "
              f"{len(truly_new_logs)} vraiment nouveaux")

        if not truly_new_logs:
            print("Aucun nouveau log depuis le dernier bloc connu — fichiers inchangés.")
            return

        first_new_ts = int(truly_new_logs[0]["timeStamp"], 16)
        first_new_date = datetime.fromtimestamp(first_new_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        new_supply, new_holders = build_daily_snapshots(truly_new_logs, balances, decimals)
        print(f"  {len(new_supply)} nouveaux jours avec activité")

        merged_supply = [pt for pt in existing_supply if pt["date"] < first_new_date] + new_supply
        merged_holders = [pt for pt in existing_holders if pt["date"] < first_new_date] + new_holders

        all_blocks = [int(l["blockNumber"], 16) for l in fetched_logs]
        new_last_block = max(all_blocks)

    else:
        # ── Full fetch (first run) ─────────────────────────────────────────────
        print("Premier fetch complet...")
        balances = {}
        fetched_logs = fetch_transfer_logs(0)
        print(f"Total: {len(fetched_logs)} événements Transfer")

        print("Calcul de la supply et du nombre de holders par jour...")
        merged_supply, merged_holders = build_daily_snapshots(fetched_logs, balances, decimals)
        print(f"  {len(merged_supply)} jours avec activité")

        all_blocks = [int(l["blockNumber"], 16) for l in fetched_logs]
        new_last_block = max(all_blocks) if all_blocks else 0

    if not merged_supply:
        print("Aucune donnée supply trouvée.")
        return

    # ── Save state + output files ──────────────────────────────────────────────
    new_state = {
        "last_block": new_last_block,
        "balances": {k: str(v) for k, v in balances.items()},
    }
    save_json(STATE_FILE, new_state)
    save_json("data/usdcv_eth_marketcap.json", merged_supply)
    save_json("data/usdcv_eth_holders.json", merged_holders)

    print(f"Sauvegardé : {len(merged_supply)} points supply, {len(merged_holders)} points holders")
    if merged_supply:
        print(f"  Dernier point : {merged_supply[-1]}")
    print(f"  État sauvegardé : bloc {new_last_block}, {len(balances)} adresses")


if __name__ == "__main__":
    main()
