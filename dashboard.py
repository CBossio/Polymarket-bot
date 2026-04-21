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
from flask import Flask, jsonify, request, render_template_string
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
    """Returns cumulative P&L time series for the chart."""
    trades = redis_mgr.get_sim_trades(limit=500)
    resolved = sorted(
        [t for t in trades if t.get("result") in ("WON", "LOST") and t.get("pnl") is not None],
        key=lambda x: x.get("timestamp", 0)
    )
    labels = []
    cumulative = []
    running = 0.0
    for t in resolved:
        ts = t.get("resolved_at") or t.get("timestamp", 0)
        labels.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M"))
        running += t.get("pnl", 0)
        cumulative.append(round(running, 2))
    return jsonify({"labels": labels, "values": cumulative})

@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    body = request.get_json() or {}
    mode = body.get("mode", "dry_run")
    redis_mgr.set_bot_mode(mode)
    return jsonify({"mode": mode, "ok": True})

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🤖</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body { background: #0f172a; color: #f1f5f9; font-family: 'Inter', sans-serif; -webkit-font-smoothing: antialiased; }
  .mono { font-family: 'JetBrains Mono', monospace; }
  
  .card { 
    background: #1e293b; 
    border: 1px solid #334155; 
    border-radius: 12px; 
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06); 
    transition: transform 0.2s, box-shadow 0.2s; 
  }
  .card:hover { transform: translateY(-2px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05); }
  
  /* Badges */
  .badge { padding: 0.15rem 0.5rem; border-radius: 9999px; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; display: inline-block; text-align: center; }
  .badge-open   { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
  .badge-won    { background: rgba(52, 211, 153, 0.15); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3); }
  .badge-lost   { background: rgba(248, 113, 113, 0.15); color: #f87171; border: 1px solid rgba(248, 113, 113, 0.3); }
  .badge-sold   { background: rgba(167, 139, 250, 0.15); color: #a78bfa; border: 1px solid rgba(167, 139, 250, 0.3); }
  .badge-buy    { background: rgba(52, 211, 153, 0.15); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3); }
  .badge-skip   { background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.3); }
  .badge-blocked{ background: rgba(248, 113, 113, 0.15); color: #f87171; border: 1px solid rgba(248, 113, 113, 0.3); }
  
  .pnl-pos { color: #34d399; }
  .pnl-neg { color: #f87171; }
  .pnl-neu { color: #94a3b8; }
  
  /* Tables */
  .table-container { max-height: 450px; overflow-y: auto; border: 1px solid #334155; border-radius: 8px; background: #1e293b; }
  table { width: 100%; border-collapse: separate; border-spacing: 0; }
  th { 
    background: #0f172a; 
    position: sticky; 
    top: 0; 
    padding: 0.75rem 1rem; 
    color: #94a3b8; 
    font-size: 0.7rem; 
    font-weight: 600; 
    text-transform: uppercase; 
    letter-spacing: 0.05em; 
    border-bottom: 1px solid #334155; 
    z-index: 10; 
  }
  td { padding: 0.75rem 1rem; font-size: 0.85rem; border-bottom: 1px solid #334155; white-space: nowrap; }
  tbody tr { transition: background-color 0.15s; }
  tbody tr:hover { background-color: rgba(255, 255, 255, 0.03); }
  tbody tr:last-child td { border-bottom: none; }
  
  /* Toggles */
  .switch { position:relative; display:inline-block; width:52px; height:26px; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#334155; border-radius:26px; transition:.3s; }
  .slider:before { position:absolute; content:""; height:18px; width:18px; left:4px; bottom:4px; background:#cbd5e1; border-radius:50%; transition:.3s; }
  input:checked + .slider { background:#059669; }
  input:checked + .slider:before { transform:translateX(26px); background:#3fb950; }
  
  /* Scrollbars */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #64748b; }
</style>
</head>
<body class="min-h-screen">

<!-- Header -->
<div class="sticky top-0 z-50 bg-slate-900/90 backdrop-blur-md px-6 py-4 border-b border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-4 shadow-sm">
  <div class="flex items-center gap-3">
    <span class="text-xl">🤖</span>
    <div>
      <h1 class="text-lg font-bold">Polymarket Bot</h1>
      <p class="text-xs text-gray-500">Delayed Mirror Strategy — Sports Markets</p>
    </div>
  </div>
  <div class="flex items-center gap-4">
    <div class="flex items-center gap-2 text-sm">
      <span class="text-slate-400 font-medium">Mode:</span>
      <label class="switch" title="Toggle DRY RUN / LIVE">
        <input type="checkbox" id="modeToggle" onchange="toggleMode(this)">
        <span class="slider"></span>
      </label>
      <span id="modeLabel" class="text-xs font-bold text-amber-400 bg-amber-400/10 px-2 py-1 rounded-md">DRY RUN</span>
    </div>
    <div id="lastUpdate" class="text-xs text-slate-500 font-medium bg-slate-800 px-3 py-1.5 rounded-full"></div>
  </div>
</div>

<!-- Kill Switch Banner -->
<div id="killBanner" class="hidden bg-rose-950/80 backdrop-blur border-b border-rose-800 text-rose-200 text-center py-3 text-sm font-bold shadow-lg">
  <span class="animate-pulse mr-2">⚠️</span> KILL SWITCH ACTIVE — Daily drawdown limit reached. Bot is paused.
</div>

<!-- Stats Cards -->
<div class="px-6 py-6 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total P&L</p>
      <span class="text-slate-500 text-lg">💰</span>
    </div>
    <p id="totalPnl" class="text-2xl font-bold pnl-neu mono">—</p>
    <p id="roiPct" class="text-xs text-slate-400 mt-2 font-medium"></p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Win Rate</p>
      <span class="text-slate-500 text-lg">🎯</span>
    </div>
    <p id="winRate" class="text-2xl font-bold text-blue-400 mono">—</p>
    <p id="wlRecord" class="text-xs text-slate-400 mt-2 font-medium"></p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Trades</p>
      <span class="text-slate-500 text-lg">📊</span>
    </div>
    <p id="totalTrades" class="text-2xl font-bold text-slate-200 mono">—</p>
    <p id="openTrades" class="text-xs text-slate-400 mt-2 font-medium"></p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Invested</p>
      <span class="text-slate-500 text-lg">💸</span>
    </div>
    <p id="totalInvested" class="text-2xl font-bold text-purple-400 mono">—</p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Daily P&L</p>
      <span class="text-slate-500 text-lg">📅</span>
    </div>
    <p id="dailyPnl" class="text-2xl font-bold pnl-neu mono">—</p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Watching</p>
      <span class="text-slate-500 text-lg">👁️</span>
    </div>
    <p id="activeObs" class="text-2xl font-bold text-cyan-400 mono">—</p>
    <p class="text-xs text-slate-400 mt-2 font-medium">markets</p>
  </div>
  <div class="card p-5">
    <div class="flex items-center justify-between mb-2">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Status</p>
      <span class="text-slate-500 text-lg">⚡</span>
    </div>
    <p id="botStatus" class="text-sm font-bold text-emerald-400 mt-2">RUNNING</p>
    <p class="text-xs text-slate-400 mt-2 font-medium">24/7</p>
  </div>
</div>

<!-- P&L Chart + Active Observations -->
<div class="px-6 pb-4 grid grid-cols-1 lg:grid-cols-3 gap-4">
  <div class="card p-5 lg:col-span-2">
    <h2 class="text-sm font-bold text-slate-300 mb-4 flex items-center gap-2"><span class="w-2 h-2 rounded-full bg-blue-500"></span> Cumulative P&L</h2>
    <div class="relative h-[350px] w-full">
      <canvas id="pnlChart"></canvas>
      <p id="chartEmpty" class="absolute inset-0 flex items-center justify-center text-slate-500 text-sm italic">No resolved trades yet</p>
    </div>
  </div>
  <div class="card p-5">
    <h2 class="text-sm font-bold text-slate-300 mb-4 flex items-center gap-2"><span class="w-2 h-2 rounded-full bg-cyan-400 animate-pulse"></span> Active Scans</h2>
    <div id="activeList" class="space-y-2 text-sm max-h-[350px] overflow-y-auto pr-1">
      <p class="text-slate-500 text-xs italic">No markets being observed right now</p>
    </div>
  </div>
</div>

<!-- Trade History -->
<div class="px-6 pb-4">
  <div class="card p-5">
    <h2 class="text-sm font-bold text-slate-300 mb-4 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-purple-500"></span> Trade History 
      <span class="text-slate-500 font-normal text-xs ml-2">(simulated & live)</span>
    </h2>
    <div class="table-container">
      <table>
        <thead><tr class="text-left">
          <th>Placed</th>
          <th>Market</th>
          <th>Link</th>
          <th>Bet on</th>
          <th>Closes</th>
          <th>Entry</th>
          <th>Bet</th>
          <th>Potential</th>
          <th>Status</th>
          <th>P&L</th>
        </tr></thead>
        <tbody id="tradeTable">
          <tr><td colspan="10" class="py-8 text-center text-slate-500 text-sm italic">No trades recorded yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- Decision Log -->
<div class="px-6 pb-8">
  <div class="card p-5">
    <h2 class="text-sm font-bold text-slate-300 mb-4 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-emerald-500"></span> Decision Log 
      <span class="text-slate-500 font-normal text-xs ml-2">(BUY / SKIP — for refinement)</span>
    </h2>
    <div class="table-container">
      <table>
        <thead><tr class="text-left">
          <th>Time</th>
          <th>Market</th>
          <th>Link</th>
          <th>Decision</th>
          <th>Mid</th>
          <th>Spread</th>
          <th>Ticks</th>
          <th>FR</th>
          <th>Size %</th>
          <th>Reason</th>
        </tr></thead>
        <tbody id="decisionTable">
          <tr><td colspan="10" class="py-8 text-center text-slate-500 text-sm italic">No decisions logged yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
let pnlChart = null;

function fmt(v, decimals=2) {
  if (v === null || v === undefined) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? '—' : n.toFixed(decimals);
}

function pnlClass(v) {
  if (v === null || v === undefined) return 'pnl-neu';
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'pnl-neu';
}

function fmtDatetime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const date = d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
  const time = d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:false});
  return `<span class="text-slate-500 mr-2">${date}</span> ${time}`;
}

function badgeHtml(result, cls) {
  return `<span class="badge badge-${cls.toLowerCase()}">${result}</span>`;
}

async function loadStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();

  const pnl = d.total_pnl;
  document.getElementById('totalPnl').textContent = (pnl >= 0 ? '+' : '') + '$' + fmt(pnl);
  document.getElementById('totalPnl').className = 'text-2xl font-bold mono ' + pnlClass(pnl);
  document.getElementById('roiPct').textContent = 'ROI: ' + (d.roi_pct >= 0 ? '+' : '') + fmt(d.roi_pct) + '%';
  document.getElementById('winRate').textContent = fmt(d.win_rate, 1) + '%';
  document.getElementById('wlRecord').textContent = d.won + 'W / ' + d.lost + 'L';
  document.getElementById('totalTrades').textContent = d.total_trades;
  document.getElementById('openTrades').textContent = d.open_trades + ' open';
  document.getElementById('totalInvested').textContent = '$' + fmt(d.total_invested);
  const dpnl = d.daily_pnl;
  document.getElementById('dailyPnl').textContent = (dpnl >= 0 ? '+' : '') + '$' + fmt(dpnl);
  document.getElementById('dailyPnl').className = 'text-2xl font-bold mono ' + pnlClass(dpnl);
  document.getElementById('activeObs').textContent = d.active_observations;

  // Mode toggle
  const isLive = d.mode === 'live';
  document.getElementById('modeToggle').checked = isLive;
  document.getElementById('modeLabel').textContent = isLive ? 'LIVE' : 'DRY RUN';
  document.getElementById('modeLabel').className = isLive ? 'text-xs font-bold text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded-md transition-colors' : 'text-xs font-bold text-amber-400 bg-amber-400/10 px-2 py-1 rounded-md transition-colors';

  // Kill switch
  document.getElementById('killBanner').classList.toggle('hidden', !d.kill_switch);

  document.getElementById('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function loadTrades() {
  const r = await fetch('/api/trades');
  const trades = await r.json();
  const tbody = document.getElementById('tradeTable');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-slate-500 text-sm italic">No trades recorded yet — waiting for first BUY signal</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const res = t.result || 'OPEN';
    const pnl = t.pnl;
    const badgeCls = res === 'WON' ? 'won' : res === 'LOST' ? 'lost' : res === 'SOLD' ? 'sold' : 'open';
    const linkHtml = t.market_url
      ? `<a href="${t.market_url}" target="_blank" class="text-blue-400 hover:text-blue-300 text-xs underline decoration-blue-500/30 underline-offset-2">↗ Link</a>`
      : '<span class="text-slate-600 text-xs">—</span>';
    let closesHtml = '<span class="text-slate-500">—</span>';
    if (t.end_date) {
      const ed = new Date(t.end_date);
      if (!isNaN(ed)) {
        const now = new Date();
        const diffH = (ed - now) / 3600000;
        const dateStr = ed.toLocaleDateString('en-US', {month:'short', day:'numeric'});
        const timeStr = ed.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:false});
        if (diffH < 0) {
          closesHtml = `<span class="text-slate-500" title="${ed.toLocaleString()}">${dateStr} <span class="text-xs text-rose-400 ml-1">ended</span></span>`;
        } else if (diffH < 6) {
          closesHtml = `<span class="text-amber-400" title="${ed.toLocaleString()}">${dateStr} ${timeStr}</span>`;
        } else {
          closesHtml = `<span class="text-slate-400" title="${ed.toLocaleString()}">${dateStr} ${timeStr}</span>`;
        }
      }
    }
    const outcome = t.picked_outcome || '—';
    return `<tr>
      <td class="mono text-slate-400">${fmtDatetime(t.timestamp)}</td>
      <td class="max-w-[220px] truncate font-medium text-slate-300" title="${t.market_question}">${(t.market_question||'').substring(0,40)}${(t.market_question||'').length>40?'…':''}</td>
      <td>${linkHtml}</td>
      <td class="text-blue-300 font-medium">${outcome}</td>
      <td class="mono">${closesHtml}</td>
      <td class="mono">${fmt(t.price * 100, 1)}%</td>
      <td class="mono font-bold text-slate-200">$${fmt(t.size_usdc)}</td>
      <td class="mono text-emerald-400">+$${fmt(t.potential_profit)}</td>
      <td>${badgeHtml(res, badgeCls)}</td>
      <td class="mono font-bold ${pnlClass(pnl)}">${pnl !== null && pnl !== undefined ? (pnl >= 0 ? '+' : '') + '$' + fmt(pnl) : '—'}</td>
    </tr>`;
  }).join('');
}

async function loadDecisions() {
  const r = await fetch('/api/decisions');
  const decs = await r.json();
  const tbody = document.getElementById('decisionTable');
  if (!decs.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-slate-500 text-sm italic">No decisions logged yet</td></tr>';
    return;
  }
  tbody.innerHTML = decs.map(d => {
    const dec = d.decision || 'SKIP';
    const cls = dec === 'BUY' ? 'buy' : dec === 'BLOCKED' ? 'blocked' : 'skip';
    const mid = d.vwap ? (d.vwap * 100).toFixed(1) + '%' : '—';
    const spread = d.spread != null ? (d.spread * 100).toFixed(1) + '%' : '—';
    const linkHtml = d.market_url
      ? `<a href="${d.market_url}" target="_blank" class="text-blue-400 hover:text-blue-300 text-xs underline decoration-blue-500/30 underline-offset-2">↗ Link</a>`
      : '<span class="text-slate-600 text-xs">—</span>';
    return `<tr>
      <td class="mono text-slate-400">${fmtDatetime(d.timestamp)}</td>
      <td class="max-w-[250px] truncate text-slate-300" title="${d.market}">${(d.market||'').substring(0,45)}${(d.market||'').length>45?'…':''}</td>
      <td>${linkHtml}</td>
      <td>${badgeHtml(dec, cls)}</td>
      <td class="mono ${d.vwap >= 0.7 ? 'text-emerald-400 font-medium' : 'text-slate-400'}">${mid}</td>
      <td class="mono ${d.spread > 0.15 ? 'text-rose-400 font-medium' : 'text-slate-400'}">${spread}</td>
      <td class="mono text-slate-400">${d.ticks ?? '—'}</td>
      <td class="mono text-slate-400">${fmt(d.focus_ratio, 0)}</td>
      <td class="mono text-slate-400">${d.fraction_pct != null ? d.fraction_pct + '%' : (d.kelly_pct != null ? d.kelly_pct + '%' : '—')}</td>
      <td class="text-slate-400 text-xs max-w-[200px] truncate" title="${d.reason}">${(d.reason||'')}</td>
    </tr>`;
  }).join('');
}

async function loadActive() {
  const r = await fetch('/api/active');
  const obs = await r.json();
  const el = document.getElementById('activeList');
  if (!obs.length) {
    el.innerHTML = '<p class="text-slate-500 text-sm italic py-4 text-center">Scanner idle — waiting for new markets</p>';
    return;
  }
  // Remove transition during initial render to avoid flash, then re-enable
  el.innerHTML = obs.map(o => {
    const urlHtml = o.url
      ? `<a href="${o.url}" target="_blank" class="text-blue-400 hover:text-blue-300 text-xs ml-1">↗</a>`
      : '';
    let ageLabel = '';
    if (o.created_at) {
      const ageMs = Date.now() - new Date(o.created_at).getTime();
      const ageH = (ageMs / 3600000).toFixed(1);
      ageLabel = `<span class="text-amber-400 font-medium">${ageH}h old</span> <span class="text-slate-600 mx-1">•</span> `;
    }
    const windowSecs = o.window_secs || 1200;
    // Bar depletes: starts at 100% (just started), reaches 0% (done)
    const initPct = Math.min(100, Math.max(0, (o.remaining_s / windowSecs) * 100));
    return `
    <div class="border border-slate-700 bg-slate-800/50 rounded-lg p-3 obs-item mb-2"
         data-started-at="${o.started_at || 0}"
         data-window-secs="${windowSecs}">
      <p class="text-sm font-medium text-slate-200 truncate mb-1.5" title="${o.question}">${(o.question||'').substring(0,55)}${urlHtml}</p>
      <div class="flex justify-between mt-1">
        <span class="text-xs text-slate-400 mono">${ageLabel}$${((o.liquidity||0)/1000).toFixed(0)}k <span class="text-slate-600 mx-1">•</span> <span class="text-indigo-400 font-medium">${o.tick_count ?? 0} ticks</span></span>
        <span class="obs-remaining text-xs font-bold text-cyan-400 mono">${o.remaining_s}s</span>
      </div>
      <div class="w-full bg-slate-900 rounded-full h-1.5 mt-2 overflow-hidden">
        <div class="obs-bar bg-gradient-to-r from-cyan-500 to-blue-500 h-full rounded-full transition-all duration-1000 ease-linear" style="width:${initPct}%"></div>
      </div>
    </div>`;
  }).join('');
  // Immediately correct bars using client-side time (avoids 1s flash of wrong width)
  updateBars();
}

function updateBars() {
  const now = Date.now() / 1000;
  document.querySelectorAll('.obs-item').forEach(item => {
    const startedAt = parseFloat(item.dataset.startedAt || 0);
    const windowSecs = parseFloat(item.dataset.windowSecs || 1200);
    const elapsed = startedAt > 0 ? (now - startedAt) : windowSecs;
    const remaining = Math.max(0, windowSecs - elapsed);
    const pct = Math.min(100, Math.max(0, (remaining / windowSecs) * 100));
    const bar = item.querySelector('.obs-bar');
    const rem = item.querySelector('.obs-remaining');
    if (bar) bar.style.width = pct + '%';
    if (rem) rem.textContent = Math.round(remaining) + 's';
  });
}

async function loadChart() {
  const r = await fetch('/api/pnl_chart');
  const d = await r.json();
  document.getElementById('chartEmpty').classList.toggle('hidden', d.values.length > 0);
  if (!d.values.length) return;

  const ctx = document.getElementById('pnlChart').getContext('2d');
  const isPositive = d.values[d.values.length-1] >= 0;
  const color = isPositive ? '#10b981' : '#f43f5e'; // Emerald vs Rose

  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.labels,
      datasets: [{
        data: d.values,
        borderColor: color,
        backgroundColor: color + '15',
        fill: true,
        tension: 0.3,
        pointRadius: 2,
        pointHoverRadius: 4,
        pointBackgroundColor: color,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { color: '#64748b', font: { family: 'JetBrains Mono', size: 10 } }, grid: { color: '#334155', drawBorder: false } },
        y: {
          ticks: {
            color: '#94a3b8', font: { family: 'JetBrains Mono', size: 10 },
            callback: v => (v >= 0 ? '+' : '') + '$' + v.toFixed(0)
          },
          grid: { color: '#334155', drawBorder: false }
        }
      }
    }
  });
}

async function toggleMode(el) {
  const mode = el.checked ? 'live' : 'dry_run';
  if (mode === 'live') {
    const ok = confirm('⚠️ Switch to LIVE mode?\n\nThis will place REAL orders on Polymarket.\nMake sure your credentials and Gnosis Safe are configured.');
    if (!ok) { el.checked = false; return; }
  }
  await fetch('/api/mode', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({mode}) });
  await loadStats();
}

let _lastResolvedCount = 0;

async function refresh() {
  await Promise.all([loadStats(), loadTrades(), loadDecisions(), loadActive()]);
  // Only redraw chart when resolved trade count changes (avoids unnecessary redraws)
  const r = await fetch('/api/trades');
  const trades = await r.json();
  const resolvedCount = trades.filter(t => t.result === 'WON' || t.result === 'LOST' || t.result === 'SOLD').length;
  if (resolvedCount !== _lastResolvedCount) {
    _lastResolvedCount = resolvedCount;
    await loadChart();
  }
}

refresh();
setInterval(refresh, 10000);   // Reload data every 10s (faster to catch resolutions)
setInterval(updateBars, 1000); // Animate progress bars every second
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

if __name__ == "__main__":
    # Start resolution checker in background
    t = threading.Thread(target=_check_resolutions, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
