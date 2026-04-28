"""
Web dashboard for the Polymarket bot.
Shows P&L, trade history, decisions, active observations.
Runs on port 5000.

Usage:
    docker exec -it polymarket-dashboard python dashboard.py
    Then open: http://localhost:5000
"""

import json
import time
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template
from redis_manager import RedisManager

app = Flask(__name__)
redis_mgr = RedisManager()

GAMMA_API = "https://gamma-api.polymarket.com"

# ── Resolution checker (runs in background) ──────────────────────────────────

CLOB_API = "https://clob.polymarket.com"

def _resolve_via_clob(token_id: str) -> str:
    """
    Returns 'WON', 'LOST', or '' (not yet resolved).
    Uses CLOB /midpoint: resolved YES → mid=1.0, resolved NO → mid=0.0.
    'No orderbook' error also means resolved — treat mid as 0 (LOST) unless we
    can confirm it's a winner via the prices-history endpoint.
    """
    try:
        r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=8)
        data = r.json()
        if "error" in data:
            # Orderbook gone — market resolved. Check last known price via history.
            h = requests.get(
                f"{CLOB_API}/prices-history",
                params={"market": token_id, "interval": "1d", "fidelity": 1},
                timeout=8,
            )
            history = h.json().get("history", [])
            last_price = float(history[-1]["p"]) if history else 0.0
            return "WON" if last_price >= 0.99 else "LOST"
        mid = float(data.get("mid", -1))
        if mid >= 0.99:
            return "WON"
        if mid <= 0.01:
            return "LOST"
    except Exception:
        pass
    return ""  # still open


def _check_resolutions():
    """Periodically checks if simulated trades have resolved and updates P&L."""
    while True:
        try:
            trades = redis_mgr.get_sim_trades(limit=200)
            open_trades = [t for t in trades if t.get("result") == "OPEN"]
            for trade in open_trades:
                token_id = trade.get("token_id", "")
                if not token_id:
                    continue
                try:
                    result = _resolve_via_clob(token_id)
                    if not result:
                        continue
                    contracts = trade.get("contracts", 0)
                    size_usdc = trade.get("size_usdc", 0)
                    pnl = round(contracts - size_usdc, 2) if result == "WON" else -round(size_usdc, 2)
                    redis_mgr.update_sim_trade(trade["trade_id"], {
                        "result": result,
                        "pnl": pnl,
                        "resolved_at": time.time(),
                    })
                    redis_mgr.remove_open_position(trade.get("condition_id", ""))
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(120)  # Check every 2 minutes


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    trades = redis_mgr.get_sim_trades(limit=500)
    resolved = [t for t in trades if t.get("result") in ("WON", "LOST")]
    open_trades = [t for t in trades if t.get("result") == "OPEN"]
    won = [t for t in resolved if t.get("result") == "WON"]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in resolved)
    invested = sum(t.get("size_usdc", 0) for t in trades)
    win_rate = len(won) / len(resolved) * 100 if resolved else 0
    risk_stats = redis_mgr.get_daily_stats() if hasattr(redis_mgr, "get_daily_stats") else {}
    return jsonify({
        "mode": redis_mgr.get_bot_mode(),
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "resolved_trades": len(resolved),
        "won": len(won),
        "lost": len(resolved) - len(won),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "total_invested": round(invested, 2),
        "roi_pct": round(total_pnl / invested * 100, 2) if invested > 0 else 0,
        "daily_pnl": risk_stats.get("pnl", 0),
        "kill_switch": risk_stats.get("kill_switch_active", False),
        "active_observations": len(redis_mgr.get_observations()),
        "timestamp": time.time(),
    })

@app.route("/api/trades")
def api_trades():
    trades = redis_mgr.get_sim_trades(limit=100)
    return jsonify(trades)

@app.route("/api/decisions")
def api_decisions():
    decisions = redis_mgr.get_decisions(limit=100)
    return jsonify(decisions)

@app.route("/api/active")
def api_active():
    obs = redis_mgr.get_observations()
    now = time.time()
    active = []
    for o in obs:
        started = o.get("started_at", now)
        elapsed = now - started
        window_secs = o.get("window_secs", 1200)
        if elapsed >= window_secs:
            continue  # stale entry — observation window already completed
        o["elapsed_s"] = int(elapsed)
        o["remaining_s"] = max(0, window_secs - int(elapsed))
        o["tick_count"] = redis_mgr.get_tick_count(o.get("token_id", ""))
        active.append(o)
    return jsonify(active)

@app.route("/api/pnl_chart")
def api_pnl_chart():
    """Returns cumulative P&L time series and details for the chart tooltips."""
    trades = redis_mgr.get_sim_trades(limit=500)
    resolved = sorted(
        [t for t in trades if t.get("result") in ("WON", "LOST") and t.get("pnl") is not None],
        key=lambda x: x.get("timestamp", 0)
    )
    labels = []
    cumulative = []
    details = []
    running = 0.0
    for t in resolved:
        ts = t.get("resolved_at") or t.get("timestamp", 0)
        labels.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M"))
        pnl = t.get("pnl", 0)
        running += pnl
        cumulative.append(round(running, 2))
        market = t.get("market_question", "Unknown Market")
        if len(market) > 40:
            market = market[:40] + "..."
        details.append(f"{market}: {'+' if pnl >= 0 else ''}${pnl:.2f}")
    return jsonify({"labels": labels, "values": cumulative, "details": details})

@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    body = request.get_json() or {}
    mode = body.get("mode", "dry_run")
    redis_mgr.set_bot_mode(mode)
    return jsonify({"mode": mode, "ok": True})

# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    # Start resolution checker in background
    t = threading.Thread(target=_check_resolutions, daemon=True)
    t.start()
    # Enabled debug=True and TEMPLATES_AUTO_RELOAD for local development
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host="0.0.0.0", port=5000, debug=True)
