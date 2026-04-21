"""
Standalone debug tool — manually watch a single Polymarket token.
Usage:  docker exec -it polymarket-python python observer.py <TOKEN_ID>

This is NOT part of the automated pipeline (which runs through main.py).
Use it to verify WebSocket connectivity and inspect live tick data for a given token.
"""

import asyncio
import json
import sys
import os
import time
import requests
import websockets

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from redis_manager import RedisManager

redis_mgr = RedisManager()
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def resolve_token(token_id: str) -> str:
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"clobTokenIds": token_id},
            timeout=10,
        )
        markets = resp.json()
        if markets:
            m = markets[0]
            outcomes = m.get("outcomes", [])
            token_ids = m.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            label = ""
            if str(token_id) in token_ids:
                idx = token_ids.index(str(token_id))
                label = outcomes[idx] if idx < len(outcomes) else ""
            return f"{m.get('question', '')} ({label})"
    except Exception as e:
        print(f"⚠️  Could not resolve token: {e}")
    return token_id[:20] + "..."


async def display_summary(token_id: str):
    while True:
        await asyncio.sleep(10)
        ticks = redis_mgr.get_recent_ticks(token_id, count=5)
        print(f"\n{'─'*50}")
        print(f"  Last {len(ticks)} ticks for {token_id[:16]}...")
        for t in ticks:
            side_icon = "🟢" if t["side"] == "BUY" else "🔴"
            print(f"  {side_icon} {t['side']:4s} | {t['price']*100:.1f}% | vol={t['size']:.2f}")
        print(f"{'─'*50}\n")


async def watch(token_id: str):
    label = resolve_token(token_id)
    print(f"\n📌  {label}")
    print(f"🎧  Listening to live ticks (Ctrl+C to stop)\n")

    summary_task = asyncio.create_task(display_summary(token_id))

    try:
        async with websockets.connect(CLOB_WS_URL, ping_interval=20) as ws:
            await ws.send(json.dumps({"assets_ids": [token_id], "type": "market"}))
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                events = data if isinstance(data, list) else [data]
                for event in events:
                        ev_type = event.get("event_type", "")

                        if "price_changes" in event:
                            for change in event.get("price_changes", []):
                                if str(change.get("asset_id", "")) != token_id:
                                    continue
                                price = float(change.get("price", 0))
                                size  = float(change.get("size", 0))
                                side  = change.get("side", "BUY").upper()
                                if price > 0:
                                    redis_mgr.save_market_tick(token_id, price, size, side)
                        elif ev_type == "price_change":
                            price = float(event.get("price", 0))
                            size  = float(event.get("size", 0))
                            side  = event.get("side", "BUY").upper()
                            if price > 0:
                                redis_mgr.save_market_tick(token_id, price, size, side)
                        elif "bids" in event or "asks" in event:
                            for side, key in [("BUY", "bids"), ("SELL", "asks")]:
                                orders = event.get(key, [])
                                if orders:
                                    best = orders[0]
                                    price = float(best["price"])
                                    size = float(best["size"])
                                    redis_mgr.save_market_tick(token_id, price, size, side)
                                    ts = time.strftime("%H:%M:%S")
                                    icon = "🟢" if side == "BUY" else "🔴"
                                    print(f"[{ts}] {icon} {side:4s} {price*100:.1f}% | vol={size:.2f}")
    except KeyboardInterrupt:
        pass
    finally:
        summary_task.cancel()
        print("\n⏹️  Debug observer stopped.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python observer.py <TOKEN_ID>")
        print("Example: python observer.py 28582846936474243462489457662900524413771862071578889675015393045047328734285")
        sys.exit(1)

    asyncio.run(watch(sys.argv[1]))
