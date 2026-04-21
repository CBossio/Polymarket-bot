"""
Find a Polymarket market that has an active CLOB order book.

Queries the CLOB API first (which has real order books), then falls back
to the Gamma API. Only the CLOB markets produce WebSocket data.

Usage:
    docker exec -it polymarket-python python find_market.py
    docker exec -it polymarket-python python find_market.py "celtics"
    docker exec -it polymarket-python python find_market.py "real madrid"
"""

import json
import sys
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


def get_clob_orderbook(token_id: str) -> dict | None:
    """Returns the order book for a token if it exists on the CLOB."""
    try:
        resp = requests.get(
            f"{CLOB_API}/order-book",
            params={"token_id": token_id},
            timeout=8,
        )
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict) and "bids" in data:
                return data
    except Exception:
        pass
    return None


def search_gamma_clob_markets(keyword: str = "") -> list:
    """
    Uses Gamma API (which has enableOrderBook + acceptingOrders + bestBid/bestAsk fields)
    to find markets that have a live CLOB order book.
    Sorted by CLOB liquidity descending.
    """
    results = []
    try:
        params = {
            "active": "true",
            "closed": "false",
            "enableOrderBook": "true",
            "acceptingOrders": "true",
            "limit": 100,
            "order": "liquidityClob",
            "ascending": "false",
        }
        resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
        resp.raise_for_status()
        markets = resp.json()

        kw = keyword.lower()
        for m in markets:
            question = m.get("question", "")
            if keyword and kw not in question.lower() and kw not in m.get("slug", "").lower():
                continue

            token_ids = m.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            if not token_ids:
                continue

            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            tokens = []
            for i, tid in enumerate(token_ids):
                tokens.append({
                    "token_id": str(tid),
                    "outcome": outcomes[i] if i < len(outcomes) else f"outcome_{i}",
                })

            results.append({
                "condition_id": m.get("conditionId", ""),
                "question": question,
                "slug": m.get("slug", ""),
                "tokens": tokens,
                "liquidity_clob": float(m.get("liquidityClob", 0) or 0),
                "volume_clob": float(m.get("volumeClob", 0) or 0),
                "best_bid": float(m.get("bestBid", 0) or 0),
                "best_ask": float(m.get("bestAsk", 0) or 0),
                "spread": float(m.get("spread", 0) or 0),
            })
    except Exception as e:
        print(f"  ⚠️  Gamma CLOB search error: {e}")
    return results


def search_clob_markets(keyword: str = "") -> list:
    """
    Fetches active markets from the CLOB API.
    Filters client-side: must be active=True, closed=False, and have volume > 0.
    Returns markets sorted by volume (highest first).
    """
    results = []
    next_cursor = ""

    for page in range(10):   # Max 10 pages
        params = {"limit": 100, "active": "true"}
        if next_cursor:
            params["next_cursor"] = next_cursor
        try:
            resp = requests.get(f"{CLOB_API}/markets", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️  CLOB API error: {e}")
            break

        markets = data.get("data", []) if isinstance(data, dict) else data
        next_cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""

        for m in markets:
            # Hard client-side filters — the CLOB API returns stale markets too
            if m.get("closed", True):
                continue
            if not m.get("active", False):
                continue

            volume = float(m.get("volume", 0) or 0)
            if volume <= 0:
                continue

            question = m.get("question", "")
            if keyword and keyword.lower() not in question.lower():
                continue

            tokens = m.get("tokens", [])
            if not tokens:
                continue

            results.append({
                "condition_id": m.get("condition_id", ""),
                "question": question,
                "tokens": tokens,
                "volume": volume,
                "active": True,
                "closed": False,
            })

        if not next_cursor or not markets:
            break

        # Stop early if we have enough results
        if not keyword and len(results) >= 20:
            break

    results.sort(key=lambda x: x["volume"], reverse=True)
    return results


def search_gamma_by_keyword(keyword: str) -> list:
    """Fallback: searches Gamma API by keyword."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 50,
                    "order": "volume", "ascending": "false"},
            timeout=10,
        )
        kw = keyword.lower()
        return [
            m for m in resp.json()
            if kw in m.get("question", "").lower() or kw in m.get("slug", "").lower()
        ]
    except Exception as e:
        print(f"  ⚠️  Gamma API error: {e}")
        return []


def print_gamma_clob_market(m: dict):
    tokens = m.get("tokens", [])
    bid = m.get("best_bid", 0)
    ask = m.get("best_ask", 0)
    liq = m.get("liquidity_clob", 0)
    vol = m.get("volume_clob", 0)

    print(f"\n  {'─'*56}")
    print(f"  Question    : {m['question'][:65]}")
    print(f"  conditionId : {m['condition_id']}")
    print(f"  CLOB Liq    : ${liq:,.2f}  |  Volume: ${vol:,.2f}")
    if bid or ask:
        print(f"  Best Bid    : {bid*100:.1f}%   Best Ask: {ask*100:.1f}%")
    print(f"  Order Book  : ✅ LIVE (enableOrderBook + acceptingOrders)")
    print()
    for t in tokens:
        tid = t.get("token_id", "")
        outcome = t.get("outcome", "?")
        marker = " ← use this in observer.py / diagnose.py" if outcome in ("Yes", "YES", "Up", "YES") else ""
        print(f"  Token [{outcome:>4s}]: {tid}{marker}")
    if m.get("slug"):
        print(f"\n  URL: https://polymarket.com/event/{m['slug']}")


def print_clob_market(m: dict, show_orderbook: bool = True):
    tokens = m.get("tokens", [])
    print(f"\n  {'─'*56}")
    print(f"  Question    : {m['question'][:65]}")
    print(f"  conditionId : {m['condition_id']}")
    print(f"  Volume      : ${m['volume']:,.0f}")
    print(f"  Active/CLOB : ✅ Yes (found in CLOB API)")
    print()

    for t in tokens:
        if isinstance(t, dict):
            tid = str(t.get("token_id", ""))
            outcome = t.get("outcome", "?")
        else:
            tid = str(t)
            outcome = "?"

        ob = get_clob_orderbook(tid) if show_orderbook else None
        ob_status = ""
        if ob is not None:
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if bids or asks:
                best_bid = f"{float(bids[0]['price'])*100:.1f}%" if bids else "—"
                best_ask = f"{float(asks[0]['price'])*100:.1f}%" if asks else "—"
                ob_status = f" | bid={best_bid} ask={best_ask} ← LIVE ORDER BOOK ✅"
            else:
                ob_status = " | order book empty (market may be inactive)"
        elif show_orderbook:
            ob_status = " | not found in CLOB order book"

        marker = " ← use this in observer.py / diagnose.py" if outcome in ("Yes", "YES", "Up") else ""
        print(f"  Token [{outcome:>4s}]: {tid}{marker}")
        if ob_status:
            print(f"               {ob_status}")

    slug = m.get("condition_id", "")
    if slug:
        print(f"\n  CLOB order book: {CLOB_API}/order-book?token_id={str(tokens[0].get('token_id','') if isinstance(tokens[0], dict) else tokens[0])[:20]}...")


def print_gamma_market(m: dict):
    token_ids = m.get("clobTokenIds", [])
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcomes = m.get("outcomes", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)

    print(f"\n  {'─'*56}")
    print(f"  Question   : {m.get('question', '')[:65]}")
    print(f"  Slug       : {m.get('slug', 'N/A')}")
    print(f"  conditionId: {m.get('conditionId', 'N/A')}")
    print(f"  Liquidity  : ${float(m.get('liquidity', 0)):,.0f}")
    print(f"  ⚠️  Source: Gamma API only — may not have CLOB order book")
    print()
    for i, tid in enumerate(token_ids):
        label = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
        print(f"  Token [{label:>4s}]: {tid}")


def main():
    keyword = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""

    if keyword:
        print(f"\n🔍 Searching CLOB for: '{keyword}'\n")
    else:
        print(f"\n🔍 Listing active CLOB markets (highest volume)\n")

    # --- Gamma API with CLOB filters (primary) ---
    clob_markets = search_gamma_clob_markets(keyword)

    if clob_markets:
        shown = clob_markets[:5]
        print(f"  Found {len(clob_markets)} market(s) with live CLOB order book. Showing top {len(shown)}:\n")
        for m in shown:
            print_gamma_clob_market(m)
    else:
        # --- Fallback: old CLOB API scan ---
        legacy = search_clob_markets(keyword)
        if legacy:
            shown = legacy[:5]
            print(f"  Found {len(legacy)} market(s) in legacy CLOB API. Showing top {len(shown)}:\n")
            for m in shown:
                print_clob_market(m, show_orderbook=False)
        elif keyword:
            print(f"  No markets with live CLOB order book found for '{keyword}'.")
            print(f"  Trying Gamma API (AMM only, no WebSocket data)...\n")
            gamma_markets = search_gamma_by_keyword(keyword)
            for m in gamma_markets[:5]:
                print_gamma_market(m)
        else:
            print("  ❌ No active CLOB markets found.")

    print(f"\n{'─'*58}")
    print("  Use the Token [Yes/Up] in:")
    print("  · python test_ws.py <TOKEN>")
    print("  · python observer.py <TOKEN>")
    print("  · python diagnose.py <TOKEN>")
    print()


if __name__ == "__main__":
    main()
