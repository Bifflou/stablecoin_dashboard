import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
MINT_ADDRESS = "DghpMkatCiUsofbTmid3M3kAbDTPqDwKiYHnudXeGG52"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SPL_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}

SUPPLY_FILE = "data/sol_marketcap.json"
HOLDERS_FILE = "data/sol_holders.json"
KNOWN_HOLDERS_FILE = "data/sol_known_holders.json"  # cache {address: first_seen_date}

LOOKBACK_DAYS = 3    # safety margin — re-fetch last N days
MAX_RETRIES = 8
BATCH_SIZE = 3       # getTransaction per batch (Helius payload limit)


# ── I/O helpers ────────────────────────────────────────────────────────────────

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
    """Single JSON-RPC call with exponential backoff on 429."""
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


def rpc_batch(requests_list):
    """Batch JSON-RPC — multiple requests in a single HTTP call."""
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": r["method"], "params": r["params"]}
        for i, r in enumerate(requests_list)
    ]
    wait = 2.0
    for attempt in range(MAX_RETRIES):
        resp = requests.post(HELIUS_RPC, json=payload, timeout=60)
        if resp.status_code == 429:
            print(f"  [429 batch] attente {wait:.0f}s (tentative {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        resp.raise_for_status()
        results = resp.json()
        results.sort(key=lambda r: r.get("id", 0))
        return [r.get("result") for r in results]
    raise RuntimeError(f"Échec batch après {MAX_RETRIES} tentatives")


# ── Decimals ───────────────────────────────────────────────────────────────────

def get_token_decimals():
    result = rpc("getAccountInfo", [MINT_ADDRESS, {"encoding": "jsonParsed"}])
    return result["value"]["data"]["parsed"]["info"]["decimals"]


# ── Signatures ────────────────────────────────────────────────────────────────

def get_signatures_since(cutoff_ts):
    """Fetch only signatures newer than cutoff_ts (epoch seconds)."""
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

        if len(recent) < len(result):   # reached older signatures
            break
        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.2)

    return list(reversed(all_sigs))     # chronological order


def get_all_signatures():
    """Full fetch — only used on first run."""
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


# ── Mint/burn extraction ──────────────────────────────────────────────────────

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
    scan(msg.get("instructions", []))
    for inner in parsed_tx.get("meta", {}).get("innerInstructions", []):
        scan(inner.get("instructions", []))

    return events


def reconstruct_supply(signatures, decimals, initial_raw=0):
    """
    Reconstruct supply history from given signatures.
    initial_raw: cumulative raw amount (before dividing by 10^decimals) to start from.
    """
    delta_by_date = defaultdict(int)
    found = 0
    total = len(signatures)

    for batch_start in range(0, total, BATCH_SIZE):
        batch = signatures[batch_start:batch_start + BATCH_SIZE]
        reqs = [
            {"method": "getTransaction", "params": [s["signature"], {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            }]}
            for s in batch
        ]
        results = rpc_batch(reqs)

        for sig_info, parsed in zip(batch, results):
            ts = sig_info.get("blockTime", 0)
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
            for ev in extract_mint_burn(parsed):
                if date:
                    delta_by_date[date] += ev["amount"] if ev["type"] == "mint" else -ev["amount"]
                    found += 1

        done = min(batch_start + BATCH_SIZE, total)
        print(f"  {done}/{total} tx analysées, {found} mint/burn trouvés")
        time.sleep(0.6)

    supply_history = []
    cumulative = initial_raw
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_tokens = cumulative / (10 ** decimals)
        supply_history.append({"date": date, "supply": round(supply_tokens, 6)})

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
        time.sleep(0.2)

    if last_sig and last_sig.get("blockTime"):
        return datetime.fromtimestamp(last_sig["blockTime"], tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def build_holders_history(token_accounts, known_holders):
    """
    Update known_holders with newly seen accounts, then rebuild full history from cache.
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

    # Rebuild from full cache (only active holders)
    active_addrs = {a.get("address", "") for a in active}
    events_by_date = defaultdict(int)
    for addr, date in known_holders.items():
        if addr in active_addrs and date:
            events_by_date[date] += 1

    holders_history = []
    cumulative = 0
    for date in sorted(events_by_date.keys()):
        cumulative += events_by_date[date]
        holders_history.append({"date": date, "holders": cumulative})

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
            "supply": round(item["supply"], 6),
        })
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    existing_supply = load_json(SUPPLY_FILE)  # list of {date, marketcap, supply}

    if existing_supply:
        # ── Incremental mode ───────────────────────────────────────────────────
        last_date = existing_supply[-1]["date"]
        cutoff_date = (
            datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")
        cutoff_ts = datetime.strptime(cutoff_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()

        # Cumulative raw supply just before the cutoff (starting point for recompute)
        baseline_supply = 0.0
        for pt in existing_supply:
            if pt["date"] < cutoff_date:
                baseline_supply = pt["supply"]
        initial_raw = int(baseline_supply * (10 ** decimals))

        print(f"Mode incrémental — signatures depuis {cutoff_date} "
              f"(baseline: {baseline_supply:,.6f})")
        signatures = get_signatures_since(cutoff_ts)
        print(f"  {len(signatures)} nouvelles signatures")

        if not signatures:
            print("Aucune nouvelle signature — fichiers inchangés.")
            return

        new_supply = reconstruct_supply(signatures, decimals, initial_raw)
        print(f"  {len(new_supply)} jours supply recalculés")

        # Keep history before cutoff, replace from cutoff onwards
        kept_supply = [pt for pt in existing_supply if pt["date"] < cutoff_date]
        # Convert kept entries to raw supply format for marketcap recomputation
        raw_supply = [{"date": pt["date"], "supply": pt["supply"]} for pt in kept_supply] + new_supply
        first_new_date = new_supply[0]["date"] if new_supply else cutoff_date

    else:
        # ── Full fetch (first run) ─────────────────────────────────────────────
        print("Premier fetch complet...")
        signatures = get_all_signatures()
        print(f"  {len(signatures)} signatures trouvées")
        raw_supply = reconstruct_supply(signatures, decimals)
        print(f"  {len(raw_supply)} jours avec activité supply")
        first_new_date = raw_supply[0]["date"] if raw_supply else None

    if not raw_supply:
        print("Aucun mintTo/burn trouvé dans les transactions.")
        return

    # ── EUR/USD + marketcap ────────────────────────────────────────────────────
    start_date = raw_supply[0]["date"]
    print(f"Récupération des taux EUR/USD depuis le {start_date}...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)
    merged_marketcap = compute_marketcap(raw_supply, eur_usd_rates)

    # ── Holders (incremental via address cache) ────────────────────────────────
    print("Récupération des comptes token (holders)...")
    token_accounts = get_token_accounts()

    known_holders = load_json(KNOWN_HOLDERS_FILE, default={})
    print("Reconstruction de l'historique des holders...")
    holders_history = build_holders_history(token_accounts, known_holders)

    # ── Save ──────────────────────────────────────────────────────────────────
    save_json(SUPPLY_FILE, merged_marketcap)
    save_json(HOLDERS_FILE, holders_history)
    save_json(KNOWN_HOLDERS_FILE, known_holders)

    print(f"Sauvegardé : {len(merged_marketcap)} points market cap, "
          f"{len(holders_history)} points holders")


if __name__ == "__main__":
    main()
