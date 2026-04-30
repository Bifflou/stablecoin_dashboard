import base64
import json
import os
import requests
import struct
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ISSUER        = "GCEYGIVOLAVBF2TG2RUSGTUJCIN75KEX3NGLMY4VPL4GFE5L355AXW3G"
ADMIN         = "GCYYFR4SR4RDSWTN64LSE4BGF2UQEDYZ32QTD7TMQXO6TXSGEDWP652D"
CONTRACT      = "CANKBYNNAYKEZXLB655F2UPNTAZFK5HILZUXL7ZTFR3NF6LKDSVY7KFH"
ASSET_CODE    = "EURCV"
HORIZON       = "https://horizon.stellar.org"
EXPERT_BASE   = "https://api.stellar.expert"
EXPERT        = f"{EXPERT_BASE}/explorer/public/asset/{ASSET_CODE}-{ISSUER}"
STELLAR_SCALE = 10 ** 7   # Stellar uses 7 decimal places

STATE_FILE = "data/stellar_state.json"
LOOKBACK = 3  # safety margin in days (unused directly — cursor-based is exact)


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def iso_to_date(iso):
    return iso[:10]


# ── XDR / ScVal decoding (no external deps) ────────────────────────────────────

SCV_I128   = 10
SCV_U128   = 9
SCV_SYMBOL = 15

def decode_scval(b64):
    """
    Decode a base64-encoded Soroban ScVal.
    Returns (type_str, python_value) or (None, None) on failure.
    """
    if not b64:
        return None, None
    try:
        raw = base64.b64decode(b64)
        discriminant = struct.unpack('>I', raw[:4])[0]

        if discriminant == SCV_SYMBOL:
            length = struct.unpack('>I', raw[4:8])[0]
            return 'symbol', raw[8:8 + length].decode('utf-8')

        elif discriminant == SCV_I128:
            hi = struct.unpack('>q', raw[4:12])[0]   # signed int64
            lo = struct.unpack('>Q', raw[12:20])[0]  # unsigned int64
            return 'i128', (hi << 64) | lo

        elif discriminant == SCV_U128:
            hi = struct.unpack('>Q', raw[4:12])[0]
            lo = struct.unpack('>Q', raw[12:20])[0]
            return 'u128', (hi << 64) | lo

    except Exception:
        pass
    return None, None


# ── Current state (stellar.expert) ─────────────────────────────────────────────

def get_asset_info():
    resp = requests.get(EXPERT, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_circulating_supply_and_holders():
    """stellar.expert /holders — sum of non-zero balances = supply, count = holders."""
    total_supply = 0.0
    holder_count = 0
    url = f"{EXPERT}/holders"

    while True:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])

        for r in records:
            bal = float(r.get("balance", 0)) / STELLAR_SCALE
            if bal > 0:
                total_supply += bal
                holder_count += 1

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break
        if next_href.startswith("/"):
            next_href = EXPERT_BASE + next_href
        url = next_href
        time.sleep(0.1)

    return round(total_supply, 7), holder_count


# ── Historical operations (Horizon) ────────────────────────────────────────────

def get_account_operations_since(account, cursor=None, label="account"):
    """
    Paginate operations on an account starting after cursor (exclusive).
    cursor=None → fetch from the beginning (oldest first).
    Returns (ops_list, last_op_id).
    """
    all_ops = []
    last_op_id = cursor

    if cursor:
        url = f"{HORIZON}/accounts/{account}/operations"
        params = {"cursor": cursor, "order": "asc", "limit": 200}
        use_params = True
        print(f"  [{label}] Mode incrémental depuis cursor {cursor}")
    else:
        url = f"{HORIZON}/accounts/{account}/operations"
        params = {"order": "asc", "limit": 200}
        use_params = True

    page = 1
    while True:
        resp = requests.get(url, params=(params if use_params else None), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])
        all_ops.extend(records)

        if records:
            last_op_id = records[-1]["id"]

        print(f"  [{label}] Page {page}: {len(records)} ops (total: {len(all_ops)})")

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break
        url = next_href
        use_params = False
        page += 1
        time.sleep(0.15)

    return all_ops, last_op_id


# ── Operation processing ────────────────────────────────────────────────────────

def process_operations(ops):
    """
    Reconstruct daily supply delta from issuer/admin operations.
    Returns (delta_by_date, holder_first_seen).
    """
    delta_by_date = defaultdict(float)
    holder_first_seen = {}

    for op in ops:
        op_type = op.get("type", "")
        date = iso_to_date(op.get("created_at", "") or "")
        if not date:
            continue

        # ── Classic payment ──────────────────────────────────────────────────
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

        # ── Soroban invoke_host_function ─────────────────────────────────────
        elif op_type == "invoke_host_function":
            if not op.get("function", "").endswith("InvokeContract"):
                continue

            params = op.get("parameters", [])
            if len(params) < 3:
                continue

            _, fn_name = decode_scval(params[1].get("value", ""))
            if fn_name not in ("mint", "mint_to_account", "burn", "clawback"):
                continue

            _, raw_amount = decode_scval(params[-1].get("value", ""))
            if raw_amount is None:
                print(f"  [warn] could not decode amount for {fn_name} on {date}")
                continue

            amount = raw_amount / STELLAR_SCALE
            print(f"  Soroban {fn_name}: {amount:,.2f} EURCV on {date}")

            if fn_name in ("mint", "mint_to_account"):
                delta_by_date[date] += amount
            else:
                delta_by_date[date] -= amount

        # ── Clawback (classic) ───────────────────────────────────────────────
        elif op_type == "clawback":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            delta_by_date[date] -= float(op.get("amount", 0))

    return delta_by_date, holder_first_seen


def build_supply_history(delta_by_date, initial_cumulative=0.0):
    supply_history = []
    cumulative = initial_cumulative
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(max(0.0, cumulative), 2)})
    return supply_history, cumulative


def build_holders_history_from_map(holder_first_seen):
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

    print("Récupération métadonnées asset via stellar.expert...")
    asset_info = get_asset_info()
    created_ts = int(asset_info.get("created", 0))
    created_date = (
        datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if created_ts else None
    )
    print(f"  Asset créé le : {created_date}")

    print("Récupération supply + holders via stellar.expert /holders...")
    current_supply, current_holders = get_circulating_supply_and_holders()
    print(f"  Circulating supply : {current_supply:,.2f} EURCV")
    print(f"  Holders actifs     : {current_holders}")

    state = load_json(STATE_FILE, default={})
    existing_marketcap = load_json("data/stellar_marketcap.json", default=[])
    existing_holders = load_json("data/stellar_holders.json", default=[])

    if state and state.get("last_op_id_issuer") and existing_marketcap:
        # ── Incremental mode ───────────────────────────────────────────────────
        cursor_issuer = state["last_op_id_issuer"]
        cursor_admin = state.get("last_op_id_admin")
        saved_holder_first_seen = state.get("holder_first_seen", {})
        supply_cumulative = float(state.get("supply_cumulative", 0.0))

        print(f"Mode incrémental — opérations issuer depuis cursor {cursor_issuer}")
        ops_issuer, new_cursor_issuer = get_account_operations_since(
            ISSUER, cursor=cursor_issuer, label="issuer"
        )
        print(f"  Issuer: {len(ops_issuer)} nouvelles opérations")

        print(f"Mode incrémental — opérations admin depuis cursor {cursor_admin}")
        ops_admin, new_cursor_admin = get_account_operations_since(
            ADMIN, cursor=cursor_admin, label="admin"
        )
        print(f"  Admin: {len(ops_admin)} nouvelles opérations")

        # Deduplicate + sort
        seen_ids = set()
        new_ops = []
        for op in ops_issuer + ops_admin:
            oid = op.get("id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                new_ops.append(op)
        new_ops.sort(key=lambda o: o.get("created_at", ""))
        print(f"  Total nouvelles opérations (dédupliquées) : {len(new_ops)}")

        if not new_ops:
            print("Aucune nouvelle opération — fichiers inchangés.")
            return

        delta_by_date, new_holder_first_seen = process_operations(new_ops)

        # Merge holder_first_seen (don't overwrite existing first-seen dates)
        merged_holder_first_seen = dict(saved_holder_first_seen)
        for addr, date in new_holder_first_seen.items():
            if addr not in merged_holder_first_seen:
                merged_holder_first_seen[addr] = date

        # Build incremental supply from cumulative base
        new_supply_history, new_cumulative = build_supply_history(delta_by_date, supply_cumulative)
        print(f"  {len(new_supply_history)} nouveaux jours avec activité supply")

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

        # Rebuild holders history from merged cache
        new_holders_history = build_holders_history_from_map(merged_holder_first_seen)
        first_new_holder_date = new_holders_history[0]["date"] if new_holders_history else today
        kept_holders = [pt for pt in existing_holders if pt["date"] < first_new_holder_date]
        merged_holders = kept_holders + new_holders_history

        new_state = {
            "last_op_id_issuer": new_cursor_issuer or cursor_issuer,
            "last_op_id_admin": new_cursor_admin or cursor_admin,
            "supply_cumulative": new_cumulative,
            "holder_first_seen": merged_holder_first_seen,
        }

    else:
        # ── Full fetch (first run) ─────────────────────────────────────────────
        print("Premier fetch complet...")

        print("Récupération des opérations issuer Stellar (Horizon)...")
        ops_issuer, last_op_id_issuer = get_account_operations_since(ISSUER, label="issuer")
        print(f"  Issuer: {len(ops_issuer)} opérations")

        print("Récupération des opérations admin Stellar (Horizon)...")
        ops_admin, last_op_id_admin = get_account_operations_since(ADMIN, label="admin")
        print(f"  Admin: {len(ops_admin)} opérations")

        # Deduplicate + sort
        seen_ids = set()
        ops = []
        for op in ops_issuer + ops_admin:
            oid = op.get("id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                ops.append(op)
        ops.sort(key=lambda o: o.get("created_at", ""))
        print(f"Total: {len(ops)} opérations (dédupliquées)")

        delta_by_date, holder_first_seen = process_operations(ops)
        merged_raw_supply, new_cumulative = build_supply_history(delta_by_date)
        merged_holders = build_holders_history_from_map(holder_first_seen)

        print(f"  {len(merged_raw_supply)} jours avec activité supply")
        print(f"  {len(merged_holders)} jours avec activité holders")

        # Fallback: flat line from creation date if no ops found
        if len(merged_raw_supply) == 0 and created_date and created_date < today:
            print("  Avertissement: aucune opération trouvée — ligne plate utilisée.")
            merged_raw_supply = [
                {"date": created_date, "supply": current_supply},
                {"date": today,        "supply": current_supply},
            ]

        new_state = {
            "last_op_id_issuer": last_op_id_issuer,
            "last_op_id_admin": last_op_id_admin,
            "supply_cumulative": new_cumulative,
            "holder_first_seen": holder_first_seen,
        }

    # ── Override today with authoritative current_supply from stellar.expert ───
    if merged_raw_supply and merged_raw_supply[-1]["date"] == today:
        merged_raw_supply[-1]["supply"] = current_supply
    else:
        merged_raw_supply.append({"date": today, "supply": current_supply})

    if merged_holders and merged_holders[-1]["date"] == today:
        merged_holders[-1]["holders"] = current_holders
    else:
        merged_holders.append({"date": today, "holders": current_holders})

    if not merged_raw_supply:
        print("Aucune donnée supply trouvée.")
        return

    # ── EUR/USD + marketcap ────────────────────────────────────────────────────
    start_date = merged_raw_supply[0]["date"]
    rates_start = (
        datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=7)
    ).strftime("%Y-%m-%d")
    print(f"Récupération taux EUR/USD depuis le {rates_start}...")
    eur_usd_rates = fetch_eur_usd_rates(rates_start)

    # Pre-seed latest rate for any supply date missing a rate
    if eur_usd_rates:
        seed_rate = eur_usd_rates[max(eur_usd_rates.keys())]
        for item in merged_raw_supply:
            if item["date"] not in eur_usd_rates:
                eur_usd_rates[item["date"]] = seed_rate

    marketcap_history = compute_marketcap(merged_raw_supply, eur_usd_rates)

    # ── Save ──────────────────────────────────────────────────────────────────
    save_json(STATE_FILE, new_state)
    save_json("data/stellar_marketcap.json", marketcap_history)
    save_json("data/stellar_holders.json", merged_holders)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, "
          f"{len(merged_holders)} points holders")
    for pt in marketcap_history[-5:]:
        print(f"  {pt['date']} : supply={pt['supply']:,.2f}  mcap=${pt['marketcap']:,.2f}")


if __name__ == "__main__":
    main()
