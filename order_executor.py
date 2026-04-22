import logging
import time
from config import (
    CLOB_API_BASE, PRIVATE_KEY, SAFE_ADDRESS,
    POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE,
)
from redis_manager import RedisManager

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    Wraps py-clob-client for placing FOK orders on Polymarket.
    Falls back to DRY RUN mode when credentials are not configured.

    Auth flow:
      - PRIVATE_KEY: the bot's session key (limited-scope key stored on VPS)
      - SAFE_ADDRESS: the Gnosis Safe that holds the USDC treasury
      - signature_type=2 (POLY_GNOSIS_SAFE) lets the relayer pay gas in MATIC
    """

    def __init__(self):
        self.dry_run = not bool(PRIVATE_KEY and POLYMARKET_API_KEY)
        self.client = None

        self._redis = RedisManager()

        if not self.dry_run:
            self._init_client()
        else:
            logger.warning(
                "[Executor] Missing credentials — running in DRY RUN mode. "
                "Set PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                "POLYMARKET_API_PASSPHRASE, and SAFE_ADDRESS in .env to go live."
            )

    def _init_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON

            self.client = ClobClient(
                host=CLOB_API_BASE,
                chain_id=POLYGON,
                key=PRIVATE_KEY,
                signature_type=2,       # POLY_GNOSIS_SAFE — enables gasless via relayer
                funder=SAFE_ADDRESS,    # Gnosis Safe holds the USDC
            )
            # Derive or load API credentials for L2 auth
            self.client.set_api_creds(
                self.client.create_or_derive_api_creds()
            )
            logger.info(f"[Executor] CLOB client initialized | Safe={SAFE_ADDRESS[:10]}...")
        except Exception as e:
            logger.error(f"[Executor] Client init failed: {e}. Falling back to DRY RUN.")
            self.dry_run = True

    def place_fok_order(
        self, token_id: str, price: float, size_usdc: float, side: str = "BUY",
        market_meta: dict = None,
    ) -> dict:
        """
        Places a Fill-or-Kill limit order.
        FOK ensures the full position is taken at `price` or the order is cancelled —
        no partial fills that would leave the strategy in an undefined state.
        """
        if self.dry_run:
            logger.info(
                f"[DRY RUN] FOK {side} {size_usdc:.2f} USDC @ {price:.4f} "
                f"on {token_id[:16]}..."
            )
        
        trade_id = f"mock_{int(time.time() * 1000)}"
        if side == "BUY":
            trade = {
                "timestamp": time.time(),
                "market_question": market_meta.get("question", "Unknown") if market_meta else "Unknown",
                "condition_id": market_meta.get("condition_id", "") if market_meta else "",
                "market_url": market_meta.get("url", "") if market_meta else "",
                "end_date": market_meta.get("end_date", "") if market_meta else "",
                "picked_outcome": market_meta.get("picked_outcome", "") if market_meta else "",
                "token_id": token_id,
                "side": side,
                "price": round(price, 4),
                "size_usdc": round(size_usdc, 2),
                "contracts": round(size_usdc / price, 4) if price > 0 else 0,
                "potential_profit": round(size_usdc * (1 - price) / price, 2) if price > 0 else 0,
                "max_loss": round(size_usdc, 2),
                "result": "OPEN",
                "pnl": None,
                "mode": "DRY_RUN" if self.dry_run else "LIVE",
            }
            trade_id = self._redis.record_sim_trade(trade)
            
            return {
                "success": True,
                "dry_run": True,
                "trade_id": trade_id,
                "token_id": token_id,
                "price": price,
                "size_usdc": size_usdc,
                "side": side,
            }

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size_usdc, 2),
                side=side,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.FOK)

            success = resp.get("success", False)
            logger.info(
                f"[Executor] Order {'FILLED' if success else 'REJECTED'} | "
                f"id={resp.get('orderID', 'N/A')} status={resp.get('status')}"
            )
            return {
                "success": success,
                "order_id": resp.get("orderID"),
                "status": resp.get("status"),
                "raw": resp,
            }

        except Exception as e:
            logger.error(f"[Executor] Order placement failed: {e}")
            return {"success": False, "error": str(e)}

    def get_open_orders(self) -> list:
        if self.dry_run or not self.client:
            return []
        try:
            return self.client.get_orders() or []
        except Exception as e:
            logger.error(f"[Executor] get_open_orders failed: {e}")
            return []

    def cancel_all_orders(self) -> bool:
        """Emergency cancel — called by kill switch or heartbeat timeout."""
        if self.dry_run or not self.client:
            return True
        try:
            self.client.cancel_all()
            logger.warning("[Executor] All open orders cancelled.")
            return True
        except Exception as e:
            logger.error(f"[Executor] cancel_all failed: {e}")
            return False
