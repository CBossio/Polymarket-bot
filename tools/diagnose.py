"""
Diagnostic script — tests the full pipeline with real Polymarket data.

Usage:
    docker exec -it polymarket-python python diagnose.py
    docker exec -it polymarket-python python diagnose.py <TOKEN_ID>
"""

import asyncio
import json
import sys
import time
import requests
import websockets
import redis as redis_lib

GAMMA_API        = "https://gamma-api.polymarket.com"
CLOB_API         = "https://clob.polymarket.com"
CLOB_WS          = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
REDIS_HOST       = "redis"
REDIS_PORT       = 6379
OBSERVATION_SECS = 30


def sep(title=""):
    width = 60
    if title:
        pad = max(0, (width - len(title) - 2) // 2)
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * width)


# ── Step 1: Redis ────────────────────────────────────────────────────────────

def check_redis() -> bool:
    sep("1 · Redis")
    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        r.ping()
        info = r.info("server")
        print(f"  ✅ Redis OK — version {info.get('redis_version')}")
        return True
    except Exception as e:
        print(f"  ❌ Redis FAILED: {e}")
        return False


# ── Step 2: Resolve token → market ──────────────────────────────────────────

def _parse_ids(raw) -> list:
    """Normalize clobTokenIds to a list of stripped strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return [raw.strip()]
    return [str(i).strip() for i in raw]


def resolve_token_to_market(token_id: str) -> dict:
    """
    Tries multiple strategies to resolve a token ID to its market.

    Strategy 1: CLOB API /markets (has direct token lookup)
    Strategy 2: Gamma API scan (pages through all markets)
    The Gamma API ?clobTokenIds filter is unreliable for large integers — ignored.
    """
    token_str = str(token_id).strip()
    fallback = {
        "condition_id": "",
        "question": "(market name not found — WebSocket will still work)",
        "outcome_label": "?",
        "token_id": token_str,
        "token_ids": [token_str],
        "outcomes": [],
        "liquidity": 0,
        "volume": 0,
    }

    # --- Strategy 1: CLOB API /markets (lists all CLOB-enabled markets) ---
    try:
        resp = requests.get(f"{CLOB_API}/markets", timeout=10)
        if resp.ok:
            data = resp.json()
            markets = data.get("data", data) if isinstance(data, dict) else data
            for m in (markets or []):
                tokens = _parse_ids(m.get("tokens", []))
                # CLOB API uses a different structure: tokens is a list of dicts
                if isinstance(m.get("tokens"), list):
                    for t in m["tokens"]:
                        if isinstance(t, dict) and str(t.get("token_id", "")).strip() == token_str:
                            outcome = t.get("outcome", "?")
                            return {
                                "condition_id": m.get("condition_id", ""),
                                "question": m.get("question", "Unknown"),
                                "outcome_label": outcome,
                                "token_id": token_str,
                                "token_ids": [str(t2.get("token_id", "")) for t2 in m.get("tokens", [])],
                                "outcomes": [t2.get("outcome", "") for t2 in m.get("tokens", [])],
                                "liquidity": 0,
                                "volume": 0,
                            }
    except Exception as e:
        print(f"  ⚠️  CLOB API lookup failed: {e}")

    # --- Strategy 2: Gamma API paginated scan ---
    # Note: ?clobTokenIds filter is unreliable for 77-digit numbers, so we scan and compare
    try:
        for offset in range(0, 200, 50):
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 50, "offset": offset},
                timeout=10,
            )
            if not resp.ok:
                break
            markets = resp.json()
            if not markets:
                break
            for m in markets:
                ids = _parse_ids(m.get("clobTokenIds", []))
                if token_str in ids:
                    outcomes = _parse_ids(m.get("outcomes", []))
                    try:
                        idx = ids.index(token_str)
                        outcome_label = outcomes[idx] if idx < len(outcomes) else "?"
                    except ValueError:
                        outcome_label = "?"
                    return {
                        "condition_id": m.get("conditionId", ""),
                        "question": m.get("question", "Unknown"),
                        "outcome_label": outcome_label,
                        "token_id": token_str,
                        "token_ids": ids,
                        "outcomes": outcomes,
                        "liquidity": float(m.get("liquidity", 0)),
                        "volume": float(m.get("volume", 0)),
                    }
    except Exception as e:
        print(f"  ⚠️  Gamma API scan failed: {e}")

    return fallback


def find_live_market() -> dict | None:
    sep("2 · Finding a live sports market")
    sports_tags = [10345, 3600, 100639, 10346]
    for tag_id in sports_tags:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "tag_id": tag_id,
                        "limit": 10, "order": "volume", "ascending": "false"},
                timeout=10,
            )
            for m in resp.json():
                liquidity = float(m.get("liquidity", 0))
                ids = _parse_ids(m.get("clobTokenIds", []))
                if liquidity >= 5_000 and ids:
                    outcomes = _parse_ids(m.get("outcomes", []))
                    print(f"  ✅ {m.get('question', '')[:65]}")
                    print(f"     Liquidity=${liquidity:,.0f} | tag={tag_id}")
                    return {
                        "condition_id": m.get("conditionId", ""),
                        "question": m.get("question", ""),
                        "outcome_label": outcomes[0] if outcomes else "YES",
                        "token_id": ids[0],
                        "token_ids": ids,
                        "outcomes": outcomes,
                        "liquidity": liquidity,
                        "volume": float(m.get("volume", 0)),
                    }
        except Exception as e:
            print(f"  ⚠️  tag {tag_id}: {e}")
    print("  ❌ No qualifying market found.")
    return None


# ── Step 3: Live WebSocket (raw + parsed) ────────────────────────────────────

async def live_observe(token_id: str, duration: int) -> list:
    sep(f"3 · Live WebSocket ({duration}s)")
    print(f"  Token : {token_id[:24]}...")
    print()

    ticks = []
    deadline = time.monotonic() + duration
    msg_count = 0
    raw_shown = 0  # Print first 3 raw messages verbatim for visibility

    try:
        async with websockets.connect(CLOB_WS, ping_interval=20, open_timeout=15) as ws:
            sub = {"assets_ids": [token_id], "type": "market"}
            await ws.send(json.dumps(sub))
            print(f"  ✅ Connected | Subscription sent: {json.dumps(sub)[:80]}\n")

            print(f"  {'Time':8s}  {'Side':5s}  {'Prob':>8s}  {'Volume':>10s}  {'Type'}")
            print(f"  {'─'*8}  {'─'*5}  {'─'*8}  {'─'*10}  {'─'*20}")

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
                    msg_count += 1

                    # Always print first 3 raw messages so we can see the actual format
                    if raw_shown < 3:
                        raw_shown += 1
                        preview = raw[:200] + ("..." if len(raw) > 200 else "")
                        print(f"  [RAW #{raw_shown}] {preview}")
                        print()

                    data = json.loads(raw)
                    events = data if isinstance(data, list) else [data]

                    for event in events:
                        # Real Polymarket WS format has NO "event" wrapper field.
                        # event_type field distinguishes price_change from book snapshots.
                        ev_type = event.get("event_type", "")
                        ts = time.strftime("%H:%M:%S")

                        if ev_type == "price_change":
                            price = float(event.get("price", 0))
                            size  = float(event.get("size", 0))
                            side  = event.get("side", "BUY").upper()
                            if price > 0:
                                icon = "🟢" if side == "BUY" else "🔴"
                                print(f"  {ts}  {icon}{side:4s}  {price*100:7.2f}%  {size:>10.4f}  price_change")
                                ticks.append({"side": side, "price": price, "size": size, "ts": time.time()})

                        elif "price_changes" in event:
                            for change in event.get("price_changes", []):
                                try:
                                    price = float(change.get("price", 0))
                                    size  = float(change.get("size", 0))
                                    side  = change.get("side", "BUY").upper()
                                    aid   = str(change.get("asset_id", ""))
                                    if price > 0:
                                        icon = "🟢" if side == "BUY" else "🔴"
                                        match = "✓" if aid == token_id else "≠"
                                        print(f"  {ts}  {icon}{side:4s}  {price*100:7.2f}%  {size:>10.4f}  price_change [{match}]")
                                        if aid == token_id:
                                            ticks.append({"side": side, "price": price, "size": size, "ts": time.time()})
                                except (KeyError, ValueError):
                                    pass

                        elif ev_type == "last_trade_price":
                            price = float(event.get("price", 0))
                            print(f"  {ts}  🔵last  {price*100:7.2f}%  {'─':>10s}  last_trade")

                        elif "bids" in event or "asks" in event:
                            # Book snapshot — bids/asks are at the top level
                            for side, key in [("BUY", "bids"), ("SELL", "asks")]:
                                orders = event.get(key, [])
                                if orders:
                                    best = orders[0]
                                    price = float(best.get("price", 0))
                                    size  = float(best.get("size", 0))
                                    if price > 0:
                                        icon = "🟢" if side == "BUY" else "🔴"
                                        print(f"  {ts}  {icon}{side:4s}  {price*100:7.2f}%  {size:>10.4f}  book_snapshot")
                                        ticks.append({"side": side, "price": price, "size": size, "ts": time.time()})
                        else:
                            preview = str(event)[:80]
                            print(f"  {ts}  ℹ️  {preview}")

                except asyncio.TimeoutError:
                    elapsed = duration - (deadline - time.monotonic())
                    pct = elapsed / duration
                    bar = "█" * int(pct * 28) + "░" * (28 - int(pct * 28))
                    secs_left = int(deadline - time.monotonic())
                    print(f"  [{bar}] {secs_left:2d}s left | msgs={msg_count} ticks={len(ticks)}   ", end="\r")

    except Exception as e:
        print(f"\n  ❌ WebSocket error: {e}")

    print()
    if msg_count == 0:
        print("  ⚠️  No messages received at all.")
        print("  Possible causes:")
        print("  · Market expired (check it's still active on polymarket.com)")
        print("  · Token ID format is wrong for this WebSocket endpoint")
        print("  · Network issue inside the container")
    elif len(ticks) == 0 and msg_count > 0:
        print(f"  ℹ️  {msg_count} message(s) received but 0 ticks parsed.")
        print("  Check the [RAW] lines above to see the actual message format.")

    return ticks


# ── Step 4: Redis ────────────────────────────────────────────────────────────

def inspect_redis(token_id: str, ticks_from_ws: list):
    sep("4 · Redis — what's stored")
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    key = f"market_ticks:{token_id}"
    stored = r.lrange(key, 0, -1)

    print(f"  Key    : market_ticks:{token_id[:20]}...")
    print(f"  Stored : {len(stored)} ticks in Redis")
    print(f"  WS saw : {len(ticks_from_ws)} ticks this run")

    if stored:
        print(f"\n  Last 5 ticks in Redis:")
        print(f"  {'Side':5s}  {'Prob':>8s}  {'Volume':>10s}  {'Age':>8s}")
        for raw in stored[:5]:
            t = json.loads(raw)
            age = time.time() - t["timestamp"]
            icon = "🟢" if t["side"] == "BUY" else "🔴"
            print(f"  {icon}{t['side']:4s}  {t['price']*100:7.2f}%  {t['size']:>10.4f}  {age:>5.0f}s ago")
    else:
        print()
        print("  ℹ️  Empty — diagnose.py doesn't write to Redis by design.")
        print("     Run observer.py <TOKEN> to populate Redis, then re-run diagnose.py.")


# ── Step 5: Brain dry-run ────────────────────────────────────────────────────

def run_brain_analysis(market: dict, ticks: list):
    sep("5 · Brain analysis (dry-run)")

    if not ticks:
        print("  ⚠️  No ticks — skipping analysis.")
        return

    buy_ticks  = [t for t in ticks if t["side"] == "BUY"]
    sell_ticks = [t for t in ticks if t["side"] == "SELL"]
    buy_vol    = sum(t["size"] for t in buy_ticks)
    vwap       = sum(t["price"] * t["size"] for t in buy_ticks) / buy_vol if buy_vol > 0 else 0.0

    print(f"  Total ticks : {len(ticks)}")
    print(f"  BUY  ticks  : {len(buy_ticks)} (vol={buy_vol:.4f})")
    print(f"  SELL ticks  : {len(sell_ticks)}")
    print(f"  VWAP prob   : {vwap*100:.2f}%  (threshold=70%)")
    print()

    if len(ticks) < 5:
        print("  Decision: ⏳ SKIP — need ≥5 ticks")
        return

    if vwap >= 0.70:
        signal_strength = (vwap - 0.70) / (1.0 - 0.70)
        fraction = min(signal_strength, 1.0) * 0.10
        capped = min(fraction, 0.10)
        print(f"  Decision  : ✅ BUY signal!")
        print(f"  Size Frac : strength={signal_strength*100:.1f}% → capped={capped*100:.2f}%")
        print(f"  On $500   : ${500*capped:.2f} USDC")
        print(f"  On $1000  : ${1000*capped:.2f} USDC")
    else:
        print(f"  Decision  : ⏳ SKIP — {vwap*100:.2f}% < 70%")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("\n" + "=" * 62)
    print("  POLYMARKET BOT — DIAGNOSTIC TOOL")
    print("  Redis · Gamma API · WebSocket · Brain logic")
    print("=" * 62)

    if not check_redis():
        return

    if len(sys.argv) >= 2:
        token_id = sys.argv[1].strip()
        sep("2 · Resolving provided token")
        print(f"  Token: {token_id[:24]}...")
        print(f"  Scanning for this token across active markets...")
        market = resolve_token_to_market(token_id)
        print(f"  Market   : {market['question'][:65]}")
        print(f"  Outcome  : {market.get('outcome_label', 'N/A')}")
        liq = market.get('liquidity', 0)
        if liq > 0:
            print(f"  Liquidity: ${liq:,.0f}")
    else:
        market = find_live_market()
        if not market:
            return
        token_id = market["token_id"]

    ticks = await live_observe(token_id, OBSERVATION_SECS)
    inspect_redis(token_id, ticks)
    run_brain_analysis(market, ticks)

    sep("Summary")
    print(f"  Market : {market['question'][:55]}")
    print(f"  Token  : {token_id[:24]}...")
    print(f"  Ticks  : {len(ticks)} in {OBSERVATION_SECS}s")
    print()
    print("  Useful commands:")
    print("  · python find_market.py <name>    → find tokens by team/sport")
    print("  · python observer.py <TOKEN>       → continuous live debug view")
    print("  · python main.py                   → full automated pipeline")
    print()


if __name__ == "__main__":
    asyncio.run(main())
