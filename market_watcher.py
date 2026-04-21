import asyncio
import json
import logging
import time
import websockets
from redis_manager import RedisManager
from config import CLOB_WS_URL, OBSERVATION_WINDOW_SECS

logger = logging.getLogger(__name__)
redis_mgr = RedisManager()

HEARTBEAT_TIMEOUT_SECS = 5


async def watch_market(token_ids, duration_secs: int = OBSERVATION_WINDOW_SECS) -> dict:
    """
    Subscribes to one or more market tokens for `duration_secs` and persists all
    price ticks to Redis keyed by each token's own ID.

    Accepts a single token_id string or a list of token_ids (YES + NO tokens).
    Returns summary stats per token.
    """
    if isinstance(token_ids, str):
        token_ids = [token_ids]
    token_ids = [str(t) for t in token_ids[:2]]  # Max 2 tokens (YES + NO)
    token_set = set(token_ids)

    ticks_by_token = {tid: 0 for tid in token_ids}
    deadline = time.monotonic() + duration_secs

    logger.info(f"[Watcher] Observing {len(token_ids)} token(s) for {duration_secs}s")

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with websockets.connect(
                CLOB_WS_URL,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                last_data_at = time.monotonic()

                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 3.0))
                        last_data_at = time.monotonic()
                        data = json.loads(raw)

                        events = data if isinstance(data, list) else [data]
                        for event in events:
                            counts = _process_event(event, token_set)
                            for tid, n in counts.items():
                                ticks_by_token[tid] = ticks_by_token.get(tid, 0) + n

                        if time.monotonic() - last_data_at > HEARTBEAT_TIMEOUT_SECS:
                            logger.warning("[Watcher] Heartbeat timeout. Reconnecting...")
                            break

                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("[Watcher] Connection closed. Reconnecting...")
                        break

        except Exception as e:
            logger.error(f"[Watcher] WebSocket error: {e}. Retrying...")
            await asyncio.sleep(1)

    total = sum(ticks_by_token.values())
    logger.info(f"[Watcher] Done. {total} total ticks | " +
                " | ".join(f"{tid[:12]}…={n}" for tid, n in ticks_by_token.items()))
    return {"ticks_by_token": ticks_by_token, "total_ticks": total}


def _process_event(event: dict, token_set: set) -> dict:
    """
    Parses a WebSocket event and saves ticks under each token's own Redis key.
    Returns dict {token_id: tick_count}.
    """
    counts = {}
    ev_type = event.get("event_type", "")

    if ev_type == "price_change":
        asset_id = str(event.get("asset_id", ""))
        if asset_id in token_set:
            try:
                price = float(event.get("price", 0))
                size  = float(event.get("size", 0))
                side  = event.get("side", "BUY").upper()
                if price > 0:
                    redis_mgr.save_market_tick(asset_id, price, size, side)
                    counts[asset_id] = counts.get(asset_id, 0) + 1
            except (KeyError, ValueError):
                pass

    elif "price_changes" in event:
        for change in event.get("price_changes", []):
            asset_id = str(change.get("asset_id", ""))
            if asset_id not in token_set:
                continue
            try:
                price = float(change.get("price", 0))
                size  = float(change.get("size", 0))
                side  = change.get("side", "BUY").upper()
                if price > 0:
                    redis_mgr.save_market_tick(asset_id, price, size, side)
                    counts[asset_id] = counts.get(asset_id, 0) + 1
            except (KeyError, ValueError):
                pass

    elif "bids" in event or "asks" in event:
        asset_id = str(event.get("asset_id", ""))
        if asset_id in token_set:
            for side, key in [("BUY", "bids"), ("SELL", "asks")]:
                orders = event.get(key, [])
                if orders:
                    try:
                        price = float(orders[0]["price"])
                        size  = float(orders[0]["size"])
                        if price > 0:
                            redis_mgr.save_market_tick(asset_id, price, size, side)
                            counts[asset_id] = counts.get(asset_id, 0) + 1
                    except (KeyError, ValueError):
                        pass

    return counts
