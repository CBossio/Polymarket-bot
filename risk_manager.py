import logging
import time
import redis
from config import REDIS_HOST, REDIS_PORT, DAILY_DRAWDOWN_KILL_PCT

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self._init_daily_record()

    def _day_key(self) -> str:
        return f"risk:daily:{time.strftime('%Y-%m-%d')}"

    def _init_daily_record(self):
        key = self._day_key()
        if not self.redis.exists(key):
            self.redis.hset(key, mapping={"pnl": 0.0, "trades": 0, "kill_switch": 0})
            self.redis.expire(key, 86400 * 2)

    def is_kill_switch_active(self) -> bool:
        self._init_daily_record()
        return int(self.redis.hget(self._day_key(), "kill_switch") or 0) == 1

    def record_trade(self, token_id: str, size_usdc: float, price: float):
        self._init_daily_record()
        self.redis.hincrbyfloat(self._day_key(), "trades", 1)
        logger.info(f"[Risk] Trade recorded — ${size_usdc:.2f} @ {price:.3f} | token={token_id[:12]}")

    def record_pnl(self, pnl: float, bankroll: float):
        """Call after market resolves. Activates kill switch if drawdown limit is hit."""
        self._init_daily_record()
        self.redis.hincrbyfloat(self._day_key(), "pnl", pnl)
        daily_pnl = float(self.redis.hget(self._day_key(), "pnl") or 0)

        if daily_pnl < 0 and bankroll > 0:
            drawdown = abs(daily_pnl) / bankroll
            if drawdown >= DAILY_DRAWDOWN_KILL_PCT:
                self.redis.hset(self._day_key(), "kill_switch", 1)
                logger.critical(
                    f"[Risk] KILL SWITCH ACTIVATED — "
                    f"drawdown={drawdown:.1%} >= limit={DAILY_DRAWDOWN_KILL_PCT:.0%}"
                )

        return daily_pnl

    def get_daily_stats(self) -> dict:
        self._init_daily_record()
        raw = self.redis.hgetall(self._day_key())
        return {
            "pnl": float(raw.get("pnl", 0)),
            "trades": int(raw.get("trades", 0)),
            "kill_switch_active": int(raw.get("kill_switch", 0)) == 1,
        }

    def reset_kill_switch(self):
        """Manual override — requires human review before calling."""
        self.redis.hset(self._day_key(), "kill_switch", 0)
        logger.warning("[Risk] Kill switch manually reset.")
