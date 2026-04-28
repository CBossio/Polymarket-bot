# Polymarket Delayed Mirror Bot

## What this project is

Automated sports prediction market bot for Polymarket using the "Delayed Mirror" (Espejo Diferido) strategy. It does NOT predict outcomes — it follows smart-money consensus after a 20-minute observation window.

**Core logic**: When a new market opens, observe it for 20 minutes. If the CLOB mid-price is within the target range (65%–85%) and all filters pass, bet using scaled-fraction sizing. The signal is the Gamma/CLOB price.

**Mode**: Currently running in **DRY RUN** (no credentials → paper trading). All trades are simulated and tracked in Redis. Set credentials in `.env` to go live.

---

## Architecture

```
main.py                 ← asyncio orchestrator (scanner + redeemer + profit-monitor loops)
├── market_scanner.py   ← Gamma API polling every 30s (finds new sports markets)
├── market_watcher.py   ← WebSocket subscriber for 20min observation window (dual-token)
├── agent_brain.py      ← LangGraph decision graph (5 nodes)
│   ├── scout           ← Loads WebSocket ticks from Redis (for FR calc only)
│   ├── analyst         ← CLOB mid-price signal + filters (65-85% target range)
│   ├── sizer           ← Scaled-fraction position sizing (scaled up to 85% consensus)
│   ├── risk_guard      ← Kill switch, liquidity, duplicate & correlation checks
│   └── executor        ← Places FOK order via py-clob-client (or records DRY RUN trade)
├── order_executor.py   ← py-clob-client wrapper + DRY RUN sim_trade recorder
├── redeemer.py         ← Checks UMA oracle + redeems winning positions
├── risk_manager.py     ← Daily PnL tracking, kill switch (5% drawdown)
├── redis_manager.py    ← All Redis persistence (ticks, wallets, positions, decisions)
├── dashboard.py        ← Flask backend (port 5000) with Auto-Reload enabled
├── templates/          ← Frontend templates
│   └── index.html      ← "Professional Terminal" UI (Public Sans & JetBrains Mono)
└── config.py           ← All constants and env vars in one place

tools/                  ← Diagnostic utilities (observer.py, diagnose.py, find_market.py, test_setup.py)
logs/decisions.jsonl    ← Persistent JSONL log of every BUY/SKIP decision
```

---

## Strategy parameters (config.py) — current values

| Parameter | Value | Description |
|---|---|---|
| `SPORTS_TAG_IDS` | `[1, 2, 3, 4, 6, 9, 64]` | Sports, Crypto, Politics, Esports, etc. |
| `MIN_LIQUIDITY_USD` | $200 | Min liquidity to START observing (new markets grow) |
| `MIN_TRADE_LIQUIDITY_USD` | $300 | Min liquidity to actually PLACE an order |
| `CONSENSUS_THRESHOLD` | 65% | Probability must exceed this to trigger BUY |
| `MAX_CONSENSUS_THRESHOLD` | 85% | Probability must NOT exceed this (protects capital from low R/R) |
| `OBSERVATION_WINDOW_SECS` | 1200 | 20-minute observation window |
| `MAX_SPREAD` | 25% | Skip if bid-ask spread > 25% (allows for early sports liquidity) |
| `MIN_PRICE_MOVE` | 2% | Required mid-price movement during window (only if initial < threshold) |
| `MIN_HOURS_TO_EVENT` | 1 | Skip markets starting in < 1h (avoids live/in-play slippage) |
| `MAX_HOURS_TO_EVENT` | 168 | Skip markets where game starts > 168h (1 week) from now |
| `MAX_MARKET_AGE_HOURS` | 72 | Ignore markets created > 72h ago |
| `MAX_POSITION_PCT` | 10% | Hard cap on bankroll per trade |
| `MAX_POSITION_USDC` | $10.0 | Hard cap in USD per single trade ($10 limit for safety) |
| `BANKROLL_USDC` | $500 | Total capital for sizing calculations |

---

## Position sizing — Scaled Fraction (Optimized)

The sizing formula scales linearly from 0% at `CONSENSUS_THRESHOLD` (65%) to `MAX_POSITION_PCT` (10%) at `MAX_CONSENSUS_THRESHOLD` (85%).

**Current formula:**
```python
signal_range = MAX_CONSENSUS_THRESHOLD - CONSENSUS_THRESHOLD
signal_strength = (p - CONSENSUS_THRESHOLD) / signal_range
fraction = min(signal_strength, 1.0) * MAX_POSITION_PCT
position_usdc = min(bankroll * fraction, MAX_POSITION_USDC)
```

Examples with $500 bankroll and $10 hard cap:
- 65% → $0 (at threshold, minimum signal)
- 75% → ~$25.00 → Capped at **$10**
- 85% → ~$50.00 → Capped at **$10**
- >85% → **SKIP** (Terrible risk/reward ratio)

---

## Filter pipeline (node_analyst)

1. CLOB mid-price fetch — if no quotes → SKIP
2. `consensus >= 0.97` → SKIP (already resolved)
3. **`consensus > 0.85` → SKIP (terrible risk/reward ratio)**
4. `spread > MAX_SPREAD (25%)` → SKIP (wide spread)
5. `focus_ratio > 500` → SKIP (HFT noise)
6. If `initial_mid < 0.65` AND `price_move < 2%` → SKIP (no conviction)
7. Multi-market correlation: if related market has consensus ≥ 60%, lower threshold from 65% to 55%
8. `consensus >= effective_threshold` → BUY, else SKIP

---

## Dashboard features (Professional Terminal UI)

- **Bloomberg-style Design**: Dark theme (`#0b0f1a`) with High-density information.
- **KPI Bar**: Instant visibility of Total P&L, Win Rate (W/L record), ROI%, and Capital Out.
- **Multi-Tab Filters**: Interactive tabs for **ALL**, **WON**, **LOST**, **SOLD**, and **OPEN** positions.
- **Real-time Search**: Search bar to filter trades by team name, outcome, or market question.
- **Performance Analytics**: P&L Curve chart with detailed tooltips (shows market name on hover).
- **Scanner Sidebar**: Live feed of active observations with progress bars and tick counters.
- **Enhanced Data**: Now includes **"CLOSES AT"** (date/time), Potential Profit, and full decision reasoning.
- **Auto-Reload**: Backend configured with `debug=True` and `TEMPLATES_AUTO_RELOAD` for instant UI updates.

---

## Running

```bash
# Start everything (standard)
docker compose up -d

# Local development with auto-reload
# 1. Start containers
docker compose up -d
# 2. Restart dashboard to enable debug mode
docker compose restart dashboard
```

---

## Status: what works

| Feature | Status |
|---|---|
| Market discovery (7 Tags: Sports, Crypto, Politics, etc.) | ✅ Ready |
| Range Trading (65%–85% target window) | ✅ Ready |
| High Contrast Terminal UI | ✅ Ready |
| Status Filters (Won/Lost/Sold/Open) | ✅ Ready |
| Live Search functionality | ✅ Ready |
| Auto-Reload for Frontend | ✅ Ready |
| Maximum Event Correlation (Max 3 bets/event) | ✅ Ready |
| Auto take-profit (2×) | ✅ Ready |
| DRY RUN paper trading + P&L tracking | ✅ Ready |
