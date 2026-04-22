import redis
import json
import os
import time
from config import REDIS_HOST, REDIS_PORT

LOG_FILE = "/app/logs/decisions.jsonl"
os.makedirs("/app/logs", exist_ok=True)

class RedisManager:
    def __init__(self):
        self.client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    def save_market_tick(self, token_id: str, price: float, size: float, side: str, wallet: str = None):
        timestamp = time.time()
        tick_data = {
            "timestamp": timestamp,
            "price": float(price),
            "size": float(size),
            "side": side,
        }
        redis_key = f"market_ticks:{token_id}"
        self.client.lpush(redis_key, json.dumps(tick_data))
        self.client.ltrim(redis_key, 0, 999)
        self.client.expire(redis_key, 600)

        if wallet:
            wallet_key = f"market_wallets:{token_id}"
            self.client.sadd(wallet_key, wallet)
            self.client.expire(wallet_key, 600)

    def get_recent_ticks(self, token_id: str, count: int = 100) -> list:
        redis_key = f"market_ticks:{token_id}"
        ticks = self.client.lrange(redis_key, 0, count - 1)
        return [json.loads(t) for t in ticks]

    def get_wallet_count(self, token_id: str) -> int:
        """Returns number of unique wallets seen (used for Focus Ratio denominator)"""
        wallet_key = f"market_wallets:{token_id}"
        return self.client.scard(wallet_key)

    def get_tick_count(self, token_id: str) -> int:
        return self.client.llen(f"market_ticks:{token_id}")

    def clear_market(self, token_id: str):
        self.client.delete(f"market_ticks:{token_id}")
        self.client.delete(f"market_wallets:{token_id}")

    def save_active_market(self, market: dict):
        """Tracks markets being observed or with open positions"""
        self.client.hset("active_markets", market["condition_id"], json.dumps(market))

    def get_active_markets(self) -> list:
        data = self.client.hgetall("active_markets")
        return [json.loads(v) for v in data.values()]

    def remove_active_market(self, condition_id: str):
        self.client.hdel("active_markets", condition_id)

    def mark_market_bet_placed(self, condition_id: str, token_id: str, size_usdc: float, price: float, contracts: float = 0, event_id: str = ""):
        """Flags that we have an open position on this market for the redeemer and profit monitor."""
        data = {
            "condition_id": condition_id,
            "token_id": token_id,
            "size_usdc": size_usdc,
            "price": price,
            "contracts": contracts or (round(size_usdc / price, 4) if price > 0 else 0),
            "timestamp": time.time(),
            "event_id": event_id,
        }
        self.client.hset("open_positions", condition_id, json.dumps(data))

    def close_position_take_profit(self, condition_id: str, sell_price: float, pnl: float):
        """Records a take-profit exit and removes position from open tracking."""
        raw = self.client.hget("open_positions", condition_id)
        if raw:
            pos = json.loads(raw)
            # Update the sim_trade if it exists in timeline
            trades = self.get_sim_trades(limit=500)
            for t in trades:
                if t.get("condition_id") == condition_id and t.get("result") == "OPEN" and t.get("side") == "BUY":
                    self.update_sim_trade(t["trade_id"], {
                        "result": "SOLD",
                        "pnl": round(pnl, 2),
                        "sell_price": round(sell_price, 4),
                        "resolved_at": time.time(),
                    })
                    break
        self.client.hdel("open_positions", condition_id)

    def get_open_positions(self) -> list:
        data = self.client.hgetall("open_positions")
        return [json.loads(v) for v in data.values()]

    def remove_open_position(self, condition_id: str):
        self.client.hdel("open_positions", condition_id)

    # Simulated / real trade recording
    def record_sim_trade(self, trade: dict) -> str:
        trade_id = f"trade:{int(time.time() * 1000)}"
        trade["trade_id"] = trade_id
        self.client.hset("sim_trades", trade_id, json.dumps(trade))
        self.client.lpush("sim_trades:timeline", trade_id)
        self.client.ltrim("sim_trades:timeline", 0, 999)
        return trade_id

    def get_sim_trades(self, limit: int = 200) -> list:
        ids = self.client.lrange("sim_trades:timeline", 0, limit - 1)
        out = []
        for tid in ids:
            raw = self.client.hget("sim_trades", tid)
            if raw:
                out.append(json.loads(raw))
        return out

    def update_sim_trade(self, trade_id: str, updates: dict):
        raw = self.client.hget("sim_trades", trade_id)
        if raw:
            t = json.loads(raw)
            t.update(updates)
            self.client.hset("sim_trades", trade_id, json.dumps(t))

    # Decision log (every BUY/SKIP with metadata for refinement)
    def log_decision(self, data: dict):
        key = f"dec:{int(time.time() * 1000)}"
        self.client.set(key, json.dumps(data), ex=86400 * 14)  # 14-day TTL
        self.client.lpush("decisions:timeline", key)
        self.client.ltrim("decisions:timeline", 0, 999)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(data) + "\n")
        except Exception:
            pass

    def get_decisions(self, limit: int = 100) -> list:
        keys = self.client.lrange("decisions:timeline", 0, limit - 1)
        out = []
        for k in keys:
            raw = self.client.get(k)
            if raw:
                out.append(json.loads(raw))
        return out

    # Bot mode: "dry_run" or "live"
    def get_bot_mode(self) -> str:
        return self.client.get("bot:mode") or "dry_run"

    def set_bot_mode(self, mode: str):
        if mode in ("dry_run", "live"):
            self.client.set("bot:mode", mode)

    # Active pipeline observations
    def set_observation(self, condition_id: str, data: dict):
        self.client.hset("observations", condition_id, json.dumps(data))
        self.client.expire("observations", 3600)  # 1h — covers 20min window + buffer

    def store_event_signal(self, event_id: str, token_id: str, consensus: float, side: str):
        """Stores this market's consensus for multi-market correlation checks."""
        if not event_id:
            return
        key = f"event_signal:{event_id}"
        self.client.hset(key, token_id, json.dumps({
            "token_id": token_id, "consensus": consensus, "side": side, "ts": time.time()
        }))
        self.client.expire(key, 3600)

    def get_event_signals(self, event_id: str) -> list:
        """Returns all signals logged for an event (for correlation check)."""
        if not event_id:
            return []
        raw = self.client.hgetall(f"event_signal:{event_id}")
        return [json.loads(v) for v in raw.values()]

    def get_observations(self) -> list:
        raw = self.client.hgetall("observations")
        return [json.loads(v) for v in raw.values()]

    def clear_observation(self, condition_id: str):
        self.client.hdel("observations", condition_id)
