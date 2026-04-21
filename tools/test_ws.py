"""
Raw WebSocket + CLOB REST diagnostic tool.

Checks:
  1. Is the token listed in the CLOB REST API? (many Gamma markets have no CLOB order book)
  2. Which WebSocket subscription format works?
  3. Do we receive actual order book data?

Usage:
    docker exec -it polymarket-python python test_ws.py <TOKEN_ID>
    docker exec -it polymarket-python python test_ws.py   # auto-finds a CLOB market
"""
import asyncio
import json
import sys
import requests
import websockets

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CLOB_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WAIT      = 5


# ── Helpers ──────────────────────────────────────────────────────────────────

def find_clob_token() -> tuple[str, str]:
    """
    Finds a valid token that actually has an active CLOB order book.
    Returns (token_id, market_question).
    """
    print("  Fetching markets from CLOB REST API...")
    try:
        resp = requests.get(f"{CLOB_API}/markets", params={"limit": 20}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        markets = data.get("data", data) if isinstance(data, dict) else data
        for m in (markets or []):
            # Skip closed/inactive markets
            if m.get("closed", True) or not m.get("active", False):
                continue
            if float(m.get("volume", 0) or 0) <= 0:
                continue
            tokens = m.get("tokens", [])
            if isinstance(tokens, list) and tokens:
                t = tokens[0]
                token_id = str(t.get("token_id", "") if isinstance(t, dict) else t)
                if token_id:
                    q = m.get("question", "Unknown")[:60]
                    print(f"  Found CLOB market: {q}")
                    print(f"  condition_id: {m.get('condition_id','?')[:20]}...")
                    print(f"  token_id    : {token_id[:32]}...")
                    return token_id, q
    except Exception as e:
        print(f"  CLOB /markets failed: {e}")

    # Fallback: Gamma API high-liquidity market
    print("  Trying Gamma API fallback...")
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 50,
                    "order": "volume", "ascending": "false"},
            timeout=10,
        )
        for m in resp.json():
            if float(m.get("liquidity", 0)) < 50_000:
                continue
            ids = m.get("clobTokenIds", [])
            if isinstance(ids, str):
                ids = json.loads(ids)
            if ids:
                tid = str(ids[0])
                q = m.get("question", "Unknown")[:60]
                print(f"  Found Gamma market (high liq): {q}")
                return tid, q
    except Exception as e:
        print(f"  Gamma fallback failed: {e}")

    return "", ""


def check_clob_rest(token_id: str) -> dict:
    """
    Checks if a token has an active CLOB order book via REST.
    Correct endpoint: GET /order-book?token_id=TOKEN
    """
    url = f"{CLOB_API}/order-book"
    try:
        resp = requests.get(url, params={"token_id": token_id}, timeout=8)
        if resp.ok:
            data = resp.json()
            # data can be {} or {"bids":[], "asks":[]} — both are valid CLOB responses
            if isinstance(data, dict) and "bids" in data:
                return {"url": f"{url}?token_id={token_id[:20]}...", "data": data}
    except Exception as e:
        print(f"  REST check error: {e}")
    return {}


# ── WebSocket probe ───────────────────────────────────────────────────────────

async def probe(ws, label: str, message) -> bool:
    payload = json.dumps(message)
    print(f"\n  [{label}]")
    print(f"  Sending : {payload[:120]}")
    await ws.send(payload)
    for i in range(WAIT):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            if raw in ("INVALID OPERATION", "INVALID MESSAGE"):
                print(f"  Server  : ❌ {raw}")
                return False
            print(f"  Server  : ✅ GOT REAL DATA: {raw[:300]}")
            return True
        except asyncio.TimeoutError:
            print(f"  ...{i+1}s")
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'='*62}")
    print(f"  WEBSOCKET + CLOB REST DIAGNOSTIC")
    print(f"{'='*62}")

    # Determine which token to test
    if len(sys.argv) >= 2:
        token = sys.argv[1].strip()
        print(f"\n  Token provided: {token[:32]}...")
    else:
        print(f"\n── Auto-finding a CLOB market ────────────────────────────────")
        token, _ = find_clob_token()
        if not token:
            print("  ❌ Could not find any CLOB market. Check network connectivity.")
            return

    # ── Step 1: CLOB REST check ──────────────────────────────────────────────
    print(f"\n── Step 1: Does this token exist in CLOB REST API? ───────────")
    clob_data = check_clob_rest(token)
    if clob_data:
        ob = clob_data["data"]
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        print(f"  ✅ Order book found at: {clob_data['url']}")
        print(f"     Bids: {len(bids)} levels | Asks: {len(asks)} levels")
        if bids:
            print(f"     Best bid: {bids[0]}")
        if asks:
            print(f"     Best ask: {asks[0]}")
    else:
        print(f"  ❌ Token NOT found in CLOB order book.")
        print(f"     This market may be Gamma-only (AMM, no CLOB order book).")
        print(f"     WebSocket subscription will return INVALID OPERATION for AMM markets.")
        print(f"\n  Finding a market that IS on the CLOB...")
        clob_token, clob_q = find_clob_token()
        if clob_token and clob_token != token:
            print(f"\n  Re-running with CLOB market: {clob_q}")
            token = clob_token
            clob_data = check_clob_rest(token)
            if clob_data:
                print(f"  ✅ Order book confirmed for new token")
            else:
                print(f"  ⚠️  Still no order book. WebSocket may require auth.")

    # ── Step 2: WebSocket subscription test ──────────────────────────────────
    print(f"\n── Step 2: WebSocket subscription formats ────────────────────")
    print(f"  Token: {token[:32]}...")

    formats = [
        ("B: assets_ids (CORRECT format)",   {"assets_ids": [token], "type": "market"}),
        ("A: asset_ids (wrong — for ref)",   {"type": "market", "asset_ids": [token]}),
        ("F: market_ids field",              {"market_ids": [token], "type": "market"}),
        ("G: id field",                      {"id": token, "type": "market"}),
    ]

    winner = None
    try:
        async with websockets.connect(CLOB_WS, open_timeout=10, ping_interval=None) as ws:
            print(f"  ✅ Connected to WebSocket\n")
            for label, msg in formats:
                got = await probe(ws, label, msg)
                if got and winner is None:
                    winner = label
                    break  # No need to test further
    except Exception as e:
        print(f"  ❌ WebSocket connection error: {e}")

    # ── Result ───────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    if winner:
        print(f"  ✅ WebSocket working! Format: {winner}")
        print(f"  All bot files are already using this format.")
    elif clob_data:
        print(f"  ⚠️  CLOB REST works but WebSocket returns INVALID OPERATION.")
        print(f"  The WebSocket might require API key authentication.")
        print(f"  See: https://docs.polymarket.com/#websocket-auth")
    else:
        print(f"  ❌ Token not on CLOB + WebSocket INVALID OPERATION.")
        print(f"  Root cause: this market uses AMM liquidity, not CLOB order books.")
        print(f"  The bot targets CLOB markets (>$10k on-book liquidity).")
        print(f"\n  To find real CLOB markets with order books:")
        print(f"  curl https://clob.polymarket.com/markets | python -m json.tool | head -100")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    asyncio.run(main())
