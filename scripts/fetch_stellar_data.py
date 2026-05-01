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
ASSET_CODE    = "EURCV"
HORIZON       = "https://horizon.stellar.org"
EXPERT_BASE   = "https://api.stellar.expert"
EXPERT        = f"{EXPERT_BASE}/explorer/public/asset/{ASSET_CODE}-{ISSUER}"
STELLAR_SCALE = 10 ** 7

STATE_FILE = "data/stellar_state.json"


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


# ── ScVal decoding (supply only — symbol + i128/u128) ──────────────────────────

SCV_I128   = 10
SCV_U128   = 9
SCV_SYMBOL = 15

def decode_scval(b64):
    if not b64:
        return None, None
    try:
        raw = base64.b64decode(b64)
        disc = struct.unpack('>I', raw[:4])[0]
        if disc == SCV_SYMBOL:
            length = struct.unpack('>I', raw[4:8])[0]
            return 'symbol', raw[8:8 + length].decode('utf-8')
        elif disc == SCV_I128:
            hi = struct.unpack('>q', raw[4:12])[0]
            lo = struct.unpack('>Q', raw[12:20])[0]
            return 'i128', (hi << 64) | lo
        elif disc == SCV_U128:
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


def get_current_supply_and_holders():
    """Authoritative current values from stellar.expert /holders."""
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
                addr = r.get("account", "")
                if addr and addr not in (ISSUER, ADMIN):
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
    all_ops = []
    last_op_id = cursor
    url = f"{HORIZON}/accounts/{account}/operations"
    params = {"order": "asc", "limit": 200}
    if cursor:
        params["cursor"] = cursor
        print(f"  [{label}] Mode incrémental depuis cursor {cursor}")
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


# ── Supply reconstruction ──────────────────────────────────────────────────────

def process_supply(ops):
    """Extract mint/burn/payment deltas for supply reconstruction."""
    delta_by_date = defaultdict(float)
    for op in ops:
        op_type = op.get("type", "")
        date = iso_to_date(op.get("created_at", "") or "")
        if not date:
            continue

        if op_type == "payment":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            amount = float(op.get("amount", 0))
            src, dst = op.get("from", ""), op.get("to", "")
            if src == ISSUER:
                delta_by_date[date] += amount
            elif dst == ISSUER:
                delta_by_date[date] -= amount

        elif op_type == "invoke_host_function":
            # Prefer Horizon-decoded asset_balance_changes (no XDR needed)
            changes = [
                c for c in op.get("asset_balance_changes") or []
                if c.get("asset_code") == ASSET_CODE and c.get("asset_issuer") == ISSUER
            ]
            if changes:
                for change in changes:
                    ctype  = change.get("type", "")
                    amount = float(change.get("amount", 0))
                    if ctype == "mint":
                        delta_by_date[date] += amount
                    elif ctype in ("burn", "clawback"):
                        delta_by_date[date] -= amount
            else:
                # Fallback: decode ScVal parameters manually
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
                print(f"  Soroban {fn_name} (ScVal fallback): {amount:,.2f} EURCV on {date}")
                if fn_name in ("mint", "mint_to_account"):
                    delta_by_date[date] += amount
                else:
                    delta_by_date[date] -= amount

        elif op_type == "clawback":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            delta_by_date[date] -= float(op.get("amount", 0))

    return delta_by_date


def build_supply_history(delta_by_date, initial_cumulative=0.0):
    history = []
    cumulative = initial_cumulative
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        history.append({"date": date, "supply": round(max(0.0, cumulative), 2)})
    return history, cumulative


# ── Holders reconstruction (balance tracking) ──────────────────────────────────

def build_holders_snapshot(ops, prev_balances=None):
    """
    Track EURCV holder count by maintaining per-account balances.

    Sources (in priority order for each op type):
    - payment           : classic transfer, direct src/dst/amount
    - invoke_host_function : Soroban SAC — uses asset_balance_changes when
                          available (mint/transfer/burn/clawback decoded by
                          Horizon, no XDR required)
    - change_trust      : trust line closed (limit=0) → balance → 0

    Returns (daily_snapshots, final_balances).
    """
    balances = dict(prev_balances) if prev_balances else {}
    events_by_date = defaultdict(list)

    for op in ops:
        op_type = op.get("type", "")
        date = iso_to_date(op.get("created_at", "") or "")
        if not date:
            continue

        if op_type == "payment":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            amount = float(op.get("amount", 0))
            src, dst = op.get("from", ""), op.get("to", "")
            events_by_date[date].append(("payment", src, dst, amount))

        elif op_type == "invoke_host_function":
            # Horizon decodes SAC balance changes — no XDR needed
            for change in op.get("asset_balance_changes") or []:
                if (change.get("asset_code") != ASSET_CODE
                        or change.get("asset_issuer") != ISSUER):
                    continue
                ctype  = change.get("type", "")
                from_  = change.get("from") or ""
                to_    = change.get("to")   or ""
                amount = float(change.get("amount", 0))
                events_by_date[date].append(("sac", ctype, from_, to_, amount))

        elif op_type == "change_trust":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            trustor = op.get("trustor", "")
            limit = float(op.get("limit", "0") or 0)
            events_by_date[date].append(("change_trust", trustor, limit))

    snapshots = []
    for date in sorted(events_by_date.keys()):
        for event in events_by_date[date]:
            if event[0] == "payment":
                _, src, dst, amount = event
                if src and src not in (ISSUER, ADMIN):
                    balances[src] = balances.get(src, 0.0) - amount
                if dst and dst not in (ISSUER, ADMIN):
                    balances[dst] = balances.get(dst, 0.0) + amount

            elif event[0] == "sac":
                _, ctype, from_, to_, amount = event
                if ctype == "mint":
                    if to_ and to_ not in (ISSUER, ADMIN):
                        balances[to_] = balances.get(to_, 0.0) + amount
                elif ctype in ("burn", "clawback"):
                    if from_ and from_ not in (ISSUER, ADMIN):
                        balances[from_] = balances.get(from_, 0.0) - amount
                elif ctype == "transfer":
                    if from_ and from_ not in (ISSUER, ADMIN):
                        balances[from_] = balances.get(from_, 0.0) - amount
                    if to_ and to_ not in (ISSUER, ADMIN):
                        balances[to_] = balances.get(to_, 0.0) + amount

            elif event[0] == "change_trust":
                _, trustor, limit = event
                if limit == 0 and trustor:
                    balances[trustor] = 0.0

        count = sum(1 for v in balances.values() if v > 0)
        snapshots.append({"date": date, "holders": count})

    return snapshots, balances


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

    print("Récupération supply + holders actuels via stellar.expert...")
    current_supply, current_holders = get_current_supply_and_holders()
    print(f"  Circulating supply : {current_supply:,.2f} EURCV")
    print(f"  Holders actifs     : {current_holders}")

    state = load_json(STATE_FILE, default={})
    existing_marketcap = load_json("data/stellar_marketcap.json", default=[])
    existing_holders   = load_json("data/stellar_holders.json",   default=[])

    if state.get("last_op_id_issuer") and existing_marketcap:
        # ── Incremental mode ───────────────────────────────────────────────────
        cursor_issuer  = state["last_op_id_issuer"]
        cursor_admin   = state.get("last_op_id_admin")
        supply_cumulative = float(state.get("supply_cumulative", 0.0))
        prev_balances  = {k: float(v) for k, v in state.get("balances", {}).items()}

        print(f"Mode incrémental — issuer depuis {cursor_issuer}")
        ops_issuer, new_cursor_issuer = get_account_operations_since(
            ISSUER, cursor=cursor_issuer, label="issuer"
        )
        print(f"Mode incrémental — admin depuis {cursor_admin}")
        ops_admin, new_cursor_admin = get_account_operations_since(
            ADMIN, cursor=cursor_admin, label="admin"
        )

        seen_ids = set()
        new_ops = []
        for op in ops_issuer + ops_admin:
            oid = op.get("id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                new_ops.append(op)
        new_ops.sort(key=lambda o: o.get("created_at", ""))
        print(f"  {len(new_ops)} nouvelles opérations")

        if not new_ops:
            print("Aucune nouvelle opération — fichiers inchangés.")
            return

        # Supply
        delta_by_date = process_supply(new_ops)
        new_supply_history, new_cumulative = build_supply_history(delta_by_date, supply_cumulative)
        first_new_supply_date = new_supply_history[0]["date"] if new_supply_history else today
        kept_marketcap = [pt for pt in existing_marketcap if pt["date"] < first_new_supply_date]
        merged_raw_supply = (
            [{"date": pt["date"], "supply": pt["supply"]} for pt in kept_marketcap]
            + new_supply_history
        )

        # Holders
        new_holders_snapshots, new_balances = build_holders_snapshot(new_ops, prev_balances)
        if new_holders_snapshots:
            first_new_holder_date = new_holders_snapshots[0]["date"]
            kept_holders = [pt for pt in existing_holders if pt["date"] < first_new_holder_date]
            merged_holders = kept_holders + new_holders_snapshots
        else:
            merged_holders = existing_holders
            new_balances = prev_balances

        new_state = {
            "last_op_id_issuer": new_cursor_issuer or cursor_issuer,
            "last_op_id_admin":  new_cursor_admin  or cursor_admin,
            "supply_cumulative": new_cumulative,
            "balances": new_balances,
        }

    else:
        # ── Full fetch (first run) ─────────────────────────────────────────────
        print("Premier fetch complet...")

        ops_issuer, last_op_id_issuer = get_account_operations_since(ISSUER, label="issuer")
        ops_admin,  last_op_id_admin  = get_account_operations_since(ADMIN,  label="admin")

        seen_ids = set()
        ops = []
        for op in ops_issuer + ops_admin:
            oid = op.get("id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                ops.append(op)
        ops.sort(key=lambda o: o.get("created_at", ""))
        print(f"Total: {len(ops)} opérations")

        # Supply
        delta_by_date = process_supply(ops)
        merged_raw_supply, new_cumulative = build_supply_history(delta_by_date)

        if not merged_raw_supply and created_date and created_date < today:
            merged_raw_supply = [
                {"date": created_date, "supply": current_supply},
                {"date": today,        "supply": current_supply},
            ]

        # Holders
        merged_holders, final_balances = build_holders_snapshot(ops)
        print(f"  {len(merged_raw_supply)} jours supply, {len(merged_holders)} jours holders")

        new_state = {
            "last_op_id_issuer": last_op_id_issuer,
            "last_op_id_admin":  last_op_id_admin,
            "supply_cumulative": new_cumulative,
            "balances": final_balances,
        }

    # ── Override today with authoritative values from stellar.expert ──────────
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

    print(f"Sauvegardé : {len(marketcap_history)} points supply, {len(merged_holders)} points holders")
    for pt in marketcap_history[-3:]:
        print(f"  {pt['date']} : supply={pt['supply']:,.2f}  mcap=${pt['marketcap']:,.2f}")


if __name__ == "__main__":
    main()
