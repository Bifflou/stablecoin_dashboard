import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
MINT_ADDRESS = "DghpMkatCiUsofbTmid3M3kAbDTPqDwKiYHnudXeGG52"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SPL_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


# ── RPC helpers ──────────────────────────────────────────────────────────────

def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = requests.post(HELIUS_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def get_token_decimals():
    result = rpc("getAccountInfo", [MINT_ADDRESS, {"encoding": "jsonParsed"}])
    return result["value"]["data"]["parsed"]["info"]["decimals"]


# ── Signatures for mint account ──────────────────────────────────────────────

def get_mint_signatures():
    all_sigs = []
    before = None

    while True:
        params = {"limit": 1000}
        if before:
            params["before"] = before

        result = rpc("getSignaturesForAddress", [MINT_ADDRESS, params])
        if not result:
            break

        all_sigs.extend(result)
        print(f"  {len(result)} signatures (total: {len(all_sigs)})")

        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.15)

    return list(reversed(all_sigs))  # oldest first


# ── Parse each transaction for mintTo / burn instructions ───────────────────

def extract_mint_burn(parsed_tx):
    """Return list of {type: mint|burn, amount: int} from a jsonParsed transaction."""
    events = []
    if not parsed_tx:
        return events

    def scan_instructions(instructions):
        for ix in instructions:
            if ix.get("programId") not in SPL_PROGRAMS:
                continue
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            ix_type = parsed.get("type", "")
            info = parsed.get("info", {})
            if info.get("mint") != MINT_ADDRESS:
                continue
            if ix_type in ("mintTo", "mintToChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "mint", "amount": int(amt)})
            elif ix_type in ("burn", "burnChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "burn", "amount": int(amt)})

    tx = parsed_tx.get("transaction", {})
    msg = tx.get("message", {})
    scan_instructions(msg.get("instructions", []))

    for inner in parsed_tx.get("meta", {}).get("innerInstructions", []):
        scan_instructions(inner.get("instructions", []))

    return events


def reconstruct_supply(signatures, decimals):
    delta_by_date = defaultdict(int)
    found = 0

    for i, sig_info in enumerate(signatures):
        sig = sig_info["signature"]
        ts = sig_info.get("blockTime", 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None

        parsed = rpc("getTransaction", [sig, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
        }])

        events = extract_mint_burn(parsed)
        for ev in events:
            if date:
                if ev["type"] == "mint":
                    delta_by_date[date] += ev["amount"]
                else:
                    delta_by_date[date] -= ev["amount"]
                found += 1

        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(signatures)} tx analysées, {found} mint/burn trouvés")

        time.sleep(0.1)

    # Build cumulative supply
    supply_history = []
    cumulative = 0

    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_tokens = cumulative / (10 ** decimals)
        supply_history.append({"date": date, "supply": round(supply_tokens, 6)})

    return supply_history


# ── Holders via Helius getTokenAccounts ─────────────────────────────────────

def get_token_accounts():
    all_accounts = []
    cursor = None

    while True:
        params = {"mint": MINT_ADDRESS, "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts", "params": params}
        resp = requests.post(HELIUS_RPC, json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json().get("result", {})

        accounts = result.get("token_accounts", [])
        all_accounts.extend(accounts)
        print(f"  {len(all_accounts)} comptes token récupérés")

        cursor = result.get("cursor")
        if not cursor or len(accounts) < 1000:
            break

        time.sleep(0.2)

    return all_accounts


def get_first_seen_date(address):
    """Find when a token account first appeared by paging to oldest signature."""
    before = None
    last_sig = None

    while True:
        params = {"limit": 1000}
        if before:
            params["before"] = before

        result = rpc("getSignaturesForAddress", [address, params])
        if not result:
            break

        last_sig = result[-1]

        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.1)

    if last_sig and last_sig.get("blockTime"):
        return datetime.fromtimestamp(last_sig["blockTime"], tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def reconstruct_holders(token_accounts):
    active = [a for a in token_accounts if float(a.get("amount", 0)) > 0]
    print(f"  {len(active)} comptes actifs à dater")

    events_by_date = defaultdict(int)

    for i, acc in enumerate(active):
        address = acc.get("address", "")
        if not address:
            continue

        date = get_first_seen_date(address)
        if date:
            events_by_date[date] += 1

        if (i + 1) % 5 == 0:
            print(f"  {i + 1}/{len(active)} comptes datés")

        time.sleep(0.1)

    holders_history = []
    cumulative = 0

    for date in sorted(events_by_date.keys()):
        cumulative += events_by_date[date]
        holders_history.append({"date": date, "holders": cumulative})

    return holders_history


# ── EUR/USD ──────────────────────────────────────────────────────────────────

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

    print("Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    print("Récupération des signatures du compte mint...")
    signatures = get_mint_signatures()
    print(f"  {len(signatures)} signatures trouvées")

    print("Analyse des transactions (mintTo / burn)...")
    supply_history = reconstruct_supply(signatures, decimals)
    print(f"  {len(supply_history)} jours avec activité supply")

    if not supply_history:
        print("Aucun mintTo/burn trouvé dans les transactions.")
        return

    start_date = supply_history[0]["date"]
    print(f"Récupération des taux EUR/USD depuis le {start_date}...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)

    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    print("Récupération des comptes token (holders)...")
    token_accounts = get_token_accounts()

    print("Reconstruction de l'historique des holders...")
    holders_history = reconstruct_holders(token_accounts)

    with open("data/sol_marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    with open("data/sol_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, {len(holders_history)} points holders")


if __name__ == "__main__":
    main()
