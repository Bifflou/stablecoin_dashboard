import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HELIUS_API_KEY       = os.environ.get("HELIUS_API_KEY")
MINT_ADDRESS         = "8smindLdDuySY6i2bStQX9o8DVhALCXCMbNxD98unx35"
HELIUS_RPC           = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SPL_PROGRAMS         = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}

SUPPLY_FILE          = "data/usdcv_sol_marketcap.json"
HOLDERS_FILE         = "data/usdcv_sol_holders.json"
KNOWN_HOLDERS_FILE   = "data/usdcv_sol_known_holders.json"  # cache {address: first_seen_date}

LOOKBACK_DAYS        = 3    # re-fetch les N derniers jours pour sécurité
MAX_RETRIES          = 8


# ── Helpers I/O ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else []

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── RPC ────────────────────────────────────────────────────────────────────────

def rpc(method, params):
    """Appel JSON-RPC unique avec retry + backoff exponentiel sur 429."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    wait = 2.0
    for attempt in range(MAX_RETRIES):
        resp = requests.post(HELIUS_RPC, json=payload, timeout=30)
        if resp.status_code == 429:
            print(f"  [429] attente {wait:.0f}s (tentative {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")
    raise RuntimeError(f"Échec après {MAX_RETRIES} tentatives pour {method}")


# ── Supply ─────────────────────────────────────────────────────────────────────

def get_token_decimals():
    result = rpc("getAccountInfo", [MINT_ADDRESS, {"encoding": "jsonParsed"}])
    return result["value"]["data"]["parsed"]["info"]["decimals"]


def get_signatures_since(cutoff_ts):
    """Récupère seulement les signatures plus récentes que cutoff_ts (epoch sec)."""
    all_sigs = []
    before = None

    while True:
        params = {"limit": 1000}
        if before:
            params["before"] = before

        result = rpc("getSignaturesForAddress", [MINT_ADDRESS, params])
        if not result:
            break

        recent = [s for s in result if (s.get("blockTime") or 0) >= cutoff_ts]
        all_sigs.extend(recent)
        print(f"  {len(recent)} récentes / {len(result)} récupérées (total: {len(all_sigs)})")

        if len(recent) < len(result):   # on a atteint des signatures plus anciennes
            break
        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.2)

    return list(reversed(all_sigs))     # ordre chronologique


def get_all_signatures():
    """Fetch complet — utilisé uniquement au premier run."""
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
        time.sleep(0.2)

    return list(reversed(all_sigs))


def extract_mint_burn(parsed_tx):
    events = []
    if not parsed_tx:
        return events

    def scan(instructions):
        for ix in instructions:
            if ix.get("programId") not in SPL_PROGRAMS:
                continue
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            ix_type = parsed.get("type", "")
            info    = parsed.get("info", {})
            if info.get("mint") != MINT_ADDRESS:
                continue
            if ix_type in ("mintTo", "mintToChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "mint", "amount": int(amt)})
            elif ix_type in ("burn", "burnChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "burn", "amount": int(amt)})

    tx  = parsed_tx.get("transaction", {})
    msg = tx.get("message", {})
    scan(msg.get("instructions", []))
    for inner in parsed_tx.get("meta", {}).get("innerInstructions", []):
        scan(inner.get("instructions", []))

    return events


def reconstruct_supply(signatures, decimals, initial_raw=0):
    """
    Reconstruit l'historique supply à partir des signatures données.
    initial_raw : cumul brut (avant division par 10^decimals) à partir duquel démarrer.
    """
    delta_by_date = defaultdict(int)
    found = 0
    total = len(signatures)

    for i, sig_info in enumerate(signatures):
        parsed = rpc("getTransaction", [sig_info["signature"], {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
        }])
        ts   = sig_info.get("blockTime", 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
        for ev in extract_mint_burn(parsed):
            if date:
                delta_by_date[date] += ev["amount"] if ev["type"] == "mint" else -ev["amount"]
                found += 1

        print(f"  {i + 1}/{total} tx analysées, {found} mint/burn trouvés")
        time.sleep(0.4)

    supply_history = []
    cumulative = initial_raw
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(cumulative / (10 ** decimals), 2)})

    return supply_history


# ── Holders ────────────────────────────────────────────────────────────────────

def get_token_accounts():
    all_accounts = []
    cursor = None

    while True:
        params = {"mint": MINT_ADDRESS, "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts", "params": params}
        resp    = requests.post(HELIUS_RPC, json=payload, timeout=60)
        resp.raise_for_status()
        result  = resp.json().get("result", {})

        accounts = result.get("token_accounts", [])
        all_accounts.extend(accounts)
        print(f"  {len(all_accounts)} comptes token récupérés")

        cursor = result.get("cursor")
        if not cursor or len(accounts) < 1000:
            break

        time.sleep(0.2)

    return all_accounts


def get_first_seen_date(address):
    before   = None
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
        time.sleep(0.2)

    if last_sig and last_sig.get("blockTime"):
        return datetime.fromtimestamp(last_sig["blockTime"], tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def build_holders_history(token_accounts, known_holders):
    """
    Met à jour known_holders avec les nouveaux comptes,
    puis reconstruit l'historique complet à partir du cache.
    """
    active = [a for a in token_accounts if float(a.get("amount", 0)) > 0]
    new_accounts = [a for a in active if a.get("address", "") not in known_holders]

    print(f"  {len(active)} comptes actifs — {len(new_accounts)} nouveaux à dater")

    for i, acc in enumerate(new_accounts):
        address = acc.get("address", "")
        if not address:
            continue
        date = get_first_seen_date(address)
        if date:
            known_holders[address] = date
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(new_accounts)} nouveaux comptes datés")
        time.sleep(0.3)

    # Reconstruction depuis le cache complet (tous holders connus actifs)
    active_addrs    = {a.get("address", "") for a in active}
    events_by_date  = defaultdict(int)
    for addr, date in known_holders.items():
        if addr in active_addrs and date:
            events_by_date[date] += 1

    holders_history = []
    cumulative = 0
    for date in sorted(events_by_date.keys()):
        cumulative += events_by_date[date]
        holders_history.append({"date": date, "holders": cumulative})

    return holders_history


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("USDCV SOL — Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    # ── Supply (mode incrémental) ──────────────────────────────────────────────
    existing_supply = load_json(SUPPLY_FILE)

    if existing_supply:
        last_date   = existing_supply[-1]["date"]
        cutoff_date = (datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        cutoff_ts   = datetime.strptime(cutoff_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()

        # Cumul brut juste avant le cutoff (base de départ pour le recalcul)
        baseline_supply = 0
        for pt in existing_supply:
            if pt["date"] < cutoff_date:
                baseline_supply = pt["supply"]
        initial_raw = int(baseline_supply * (10 ** decimals))

        print(f"Mode incrémental — signatures depuis {cutoff_date} (baseline: {baseline_supply:,.2f})")
        signatures = get_signatures_since(cutoff_ts)
        print(f"  {len(signatures)} nouvelles signatures")

        new_supply = reconstruct_supply(signatures, decimals, initial_raw)

        # Conserver l'historique avant cutoff, remplacer depuis cutoff
        merged_supply = [pt for pt in existing_supply if pt["date"] < cutoff_date] + new_supply
    else:
        print("Premier fetch complet...")
        signatures    = get_all_signatures()
        print(f"  {len(signatures)} signatures trouvées")
        merged_supply = reconstruct_supply(signatures, decimals)

    if not merged_supply:
        print("Aucun mintTo/burn trouvé.")
        return

    print(f"  {len(merged_supply)} jours supply — dernier: {merged_supply[-1]}")

    # ── Holders (mode incrémental via cache d'adresses) ───────────────────────
    print("Récupération des comptes token (holders)...")
    token_accounts = get_token_accounts()

    known_holders = load_json(KNOWN_HOLDERS_FILE, default={})
    print("Reconstruction de l'historique des holders...")
    holders_history = build_holders_history(token_accounts, known_holders)

    # ── Sauvegarde ────────────────────────────────────────────────────────────
    save_json(SUPPLY_FILE,        merged_supply)
    save_json(HOLDERS_FILE,       holders_history)
    save_json(KNOWN_HOLDERS_FILE, known_holders)

    print(f"Sauvegardé : {len(merged_supply)} points supply, {len(holders_history)} points holders")


if __name__ == "__main__":
    main()
