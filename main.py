"""
Polymarket Delayed Mirror Bot — main entry point.

Pipeline per new market:
  1. Scanner detects qualifying sports market (Gamma API, every 30s)
  2. Watcher subscribes to WebSocket for 120s observation window
  3. Brain (LangGraph) analyzes ticks: VWAP > 70%, Focus Ratio filter, Scaled Fraction sizing
  4. Executor places FOK order if all conditions are met
  5. Redeemer checks open positions every 5 min and redeems resolved markets
"""

import asyncio
import logging
import signal
import time
import requests
from market_scanner import scan_for_sports_markets
from market_watcher import watch_market
from agent_brain import run_brain_for_market
from order_executor import OrderExecutor
from redeemer import Redeemer
from risk_manager import RiskManager
from redis_manager import RedisManager
from config import (
    SCANNER_INTERVAL_SECS, BANKROLL_USDC,
    TAKE_PROFIT_MULTIPLIER, STOP_LOSS_THRESHOLD, PROFIT_CHECK_INTERVAL_SECS, GAMMA_API_BASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

redis_mgr = RedisManager()
risk_mgr = RiskManager()
executor = OrderExecutor()
redeemer = Redeemer(executor)

_active_tasks: dict[str, asyncio.Task] = {}
_shutdown = False


async def process_market(market: dict):
    """Full observation + decision + execution pipeline for one market."""
    cid = market["condition_id"]
    question = market["question"]

    logger.info(f"\n{'─'*60}")
    logger.info(f"[Pipeline] {question[:65]}")
    logger.info(f"[Pipeline] Liquidity=${market['liquidity']:,.0f} | token={market['token_id'][:16]}...")

    redis_mgr.save_active_market(market)
    bid = market.get("best_bid", 0)
    ask = market.get("best_ask", 0)
    initial_mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else 0.0
    market["initial_mid"] = initial_mid

    redis_mgr.set_observation(market["condition_id"], {
        "question": market["question"],
        "liquidity": market["liquidity"],
        "token_id": market["token_id"],
        "url": market.get("url", ""),
        "created_at": market.get("created_at", ""),
        "hours_to_event": market.get("hours_to_event", ""),
        "initial_mid": initial_mid,
        "started_at": time.time(),
        "window_secs": 1200,
    })

    try:
        await watch_market(market.get("token_ids", [market["token_id"]]))
        result = run_brain_for_market(market, BANKROLL_USDC)

        decision = result.get("decision", "SKIP")
        reason = result.get("reason", "")
        logger.info(f"[Pipeline] → {decision} | {reason}")

        if result.get("order_result", {}) and result["order_result"].get("success"):
            pnl_estimate = result["position_size_usdc"] * (result["avg_probability"] - result["avg_probability"])
            logger.info(f"[Pipeline] Open position recorded for auto-redeem.")

        redis_mgr.clear_observation(market["condition_id"])

    except Exception as e:
        logger.error(f"[Pipeline] Error processing {cid[:12]}: {e}")
    finally:
        _active_tasks.pop(cid, None)


async def redeemer_loop():
    """Checks open positions every 5 minutes for resolution + redemption."""
    while not _shutdown:
        await asyncio.sleep(300)
        positions = redis_mgr.get_open_positions()
        if not positions:
            continue
        logger.info(f"[Redeemer] Checking {len(positions)} open position(s)...")
        for pos in positions:
            cid = pos["condition_id"]
            result = redeemer.check_and_redeem(cid, pos.get("token_id"))
            if result.get("redeemed"):
                redis_mgr.remove_open_position(cid)
                redis_mgr.remove_active_market(cid)


async def scanner_loop():
    """Polls Gamma API every 30s and spawns observation tasks for new markets."""
    logger.info("[Scanner] Starting...")

    while not _shutdown:
        stats = risk_mgr.get_daily_stats()

        if stats["kill_switch_active"]:
            logger.warning(
                f"[Scanner] Kill switch ACTIVE. "
                f"Daily PnL=${stats['pnl']:.2f} | Trades={stats['trades']}. "
                "Pausing. Manual reset required."
            )
            await asyncio.sleep(SCANNER_INTERVAL_SECS)
            continue

        logger.info(
            f"[Scanner] Scanning... | Daily PnL=${stats['pnl']:.2f} | "
            f"Trades={stats['trades']} | Active tasks={len(_active_tasks)}"
        )

        new_markets = scan_for_sports_markets()

        for market in new_markets:
            cid = market["condition_id"]
            if cid in _active_tasks:
                continue
            task = asyncio.create_task(process_market(market))
            _active_tasks[cid] = task

        await asyncio.sleep(SCANNER_INTERVAL_SECS)


def _get_current_bid(token_id: str) -> float:
    """Gets the current best bid price for a token from Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA_API_BASE}/markets",
            params={"clobTokenIds": token_id},
            timeout=8,
        )
        markets = resp.json()
        if markets:
            return float(markets[0].get("bestBid", 0) or 0)
    except Exception:
        pass
    return 0.0


async def profit_monitor_loop():
    """Checks open positions every minute and takes profit or cuts losses."""
    while not _shutdown:
        await asyncio.sleep(PROFIT_CHECK_INTERVAL_SECS)
        positions = redis_mgr.get_open_positions()
        if not positions:
            continue
        for pos in positions:
            token_id = pos.get("token_id", "")
            entry_price = pos.get("price", 0)
            contracts = pos.get("contracts", 0)
            if not token_id or entry_price <= 0 or contracts <= 0:
                continue

            target_price = entry_price * TAKE_PROFIT_MULTIPLIER
            current_price = _get_current_bid(token_id)
            if current_price <= 0:
                continue

            logger.debug(
                f"[TakeProfit] {pos['condition_id'][:12]} | "
                f"entry={entry_price:.3f} current={current_price:.3f} target={target_price:.3f}"
            )

            if current_price >= target_price:
                logger.info(
                    f"[TakeProfit] 🎯 {pos['condition_id'][:12]} | "
                    f"{entry_price:.3f} → {current_price:.3f} ({current_price/entry_price:.1f}x) — SELLING"
                )
                sell_size = round(contracts * current_price, 2)
                result = executor.place_fok_order(
                    token_id=token_id,
                    price=current_price,
                    size_usdc=sell_size,
                    side="SELL",
                    market_meta={"condition_id": pos["condition_id"], "question": "Take-profit SELL"},
                )
                if result.get("success"):
                    pnl = round(contracts * current_price - pos.get("size_usdc", 0), 2)
                    redis_mgr.close_position_take_profit(pos["condition_id"], current_price, pnl)
                    logger.info(f"[TakeProfit] Closed. PnL=+${pnl:.2f}")
                else:
                    logger.warning(f"[TakeProfit] SELL FOK failed (price moved): {result.get('error')}")

        elif current_price <= STOP_LOSS_THRESHOLD:
            logger.info(
                f"[StopLoss] 🛑 {pos['condition_id'][:12]} | "
                f"{entry_price:.3f} → {current_price:.3f} (dropped below {STOP_LOSS_THRESHOLD:.2f}) — CUTTING LOSSES"
            )
            sell_size = round(contracts * current_price, 2)
            result = executor.place_fok_order(
                token_id=token_id,
                price=current_price,
                size_usdc=sell_size,
                side="SELL",
                market_meta={"condition_id": pos["condition_id"], "question": "Stop-loss SELL"},
            )
            if result.get("success"):
                pnl = round(contracts * current_price - pos.get("size_usdc", 0), 2)
                redis_mgr.close_position_take_profit(pos["condition_id"], current_price, pnl)
                logger.info(f"[StopLoss] Closed. PnL=${pnl:.2f}")
            else:
                logger.warning(f"[StopLoss] SELL FOK failed: {result.get('error')}")


def _handle_shutdown(sig, frame):
    global _shutdown
    logger.warning(f"[Main] Received signal {sig} — initiating graceful shutdown...")
    _shutdown = True
    executor.cancel_all_orders()


async def main():
    print("\n" + "=" * 62)
    print("  POLYMARKET DELAYED MIRROR BOT  —  Sports Markets POC")
    print("=" * 62)
    print(f"  Bankroll : ${BANKROLL_USDC:.2f} USDC")
    print(f"  Strategy : 120s observation + 65% consensus threshold")
    print(f"  Execution: {'DRY RUN (no credentials)' if executor.dry_run else 'LIVE — orders will be placed'}")
    print("=" * 62 + "\n")

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    await asyncio.gather(
        scanner_loop(),
        redeemer_loop(),
        profit_monitor_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
