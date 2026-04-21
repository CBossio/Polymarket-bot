# Polymarket Delayed Mirror Bot

## What this project is

Automated sports prediction market bot for Polymarket using the "Delayed Mirror" (Espejo Diferido) strategy. It does NOT predict outcomes — it follows smart-money consensus after a 20-minute observation window.

**Core logic**: When a new market opens, observe it for 20 minutes. If the CLOB mid-price > 70% and all filters pass, bet using scaled-fraction sizing. The signal is the Gamma/CLOB price (not WebSocket VWAP — see critical bugs section below).

**Mode**: Currently running in **DRY RUN** (no credentials → paper trading). All trades are simulated and tracked in Redis. Set credentials in `.env` to go live.

---

## Architecture

```
main.py                 ← asyncio orchestrator (scanner + redeemer + profit-monitor loops)
├── market_scanner.py   ← Gamma API polling every 30s (finds new sports markets)
├── market_watcher.py   ← WebSocket subscriber for 20min observation window (dual-token)
├── agent_brain.py      ← LangGraph decision graph (5 nodes)
│   ├── scout           ← Loads WebSocket ticks from Redis (for FR calc only)
│   ├── analyst         ← CLOB mid-price signal + filters
│   ├── sizer           ← Scaled-fraction position sizing (0–10% of bankroll, max $10)
│   ├── risk_guard      ← Kill switch, liquidity, duplicate & correlation checks
│   └── executor        ← Places FOK order via py-clob-client (or records DRY RUN trade)
├── order_executor.py   ← py-clob-client wrapper + DRY RUN sim_trade recorder
├── redeemer.py         ← Checks UMA oracle + redeems winning positions
├── risk_manager.py     ← Daily PnL tracking, kill switch (5% drawdown)
├── redis_manager.py    ← All Redis persistence (ticks, wallets, positions, decisions)
├── dashboard.py        ← Flask web dashboard on port 5000 (Dark Glassmorphism UI)
└── config.py           ← All constants and env vars in one place

tools/                  ← Diagnostic utilities (observer.py, diagnose.py, find_market.py, test_setup.py)
logs/decisions.jsonl    ← Persistent JSONL log of every BUY/SKIP decision
```

---

## Strategy parameters (config.py) — current values

| Parameter | Value | Description |
|---|---|---|
| `SPORTS_TAG_IDS` | `[1]` | tag_id=1 = all Sports on Polymarket |
| `MIN_LIQUIDITY_USD` | $100 | Min liquidity to START observing (new markets grow) |
| `MIN_TRADE_LIQUIDITY_USD` | $500 | Min liquidity to actually PLACE an order |
| `CONSENSUS_THRESHOLD` | 70% | CLOB mid-price must exceed this to trigger BUY |
| `OBSERVATION_WINDOW_SECS` | 1200 | 20-minute observation window |
| `MAX_SPREAD` | 15% | Skip if bid-ask spread > 15% |
| `MIN_PRICE_MOVE` | 3% | Required mid-price movement during window (only if initial < threshold) |
| `MIN_HOURS_TO_EVENT` | 1 | Skip markets starting in < 1h (avoids live/in-play slippage) |
| `MAX_HOURS_TO_EVENT` | 168 | Skip markets where game starts > 168h (1 week) from now |
| `MAX_MARKET_AGE_HOURS` | 24 | Ignore markets created > 24h ago |
| `FOCUS_RATIO_NOISE_THRESHOLD` | 500 | FR above this = HFT bots, skip |
| `MAX_POSITION_PCT` | 10% | Hard cap on bankroll per trade |
| `MAX_POSITION_USDC` | $10.0 | Hard cap in USD per single trade |
| `CORR_BOOST_THRESHOLD` | 60% | Correlated market threshold to lower effective entry threshold |
| `DAILY_DRAWDOWN_KILL_PCT` | 5% | Kill switch threshold |
| `TAKE_PROFIT_MULTIPLIER` | 2.0 | Auto-sell when position value reaches 2× entry |
| `PROFIT_CHECK_INTERVAL_SECS` | 60 | How often to check for take-profit |
| `SCANNER_INTERVAL_SECS` | 30 | How often to poll Gamma API |
| `BANKROLL_USDC` | $500 | Set in .env |

---

## Position sizing — Scaled Fraction (NOT Kelly)

Kelly was completely removed because it requires an independent probability estimate. Using the market price as both signal and probability mathematically eliminates edge.

**Current formula:**
```python
signal_strength = (consensus - CONSENSUS_THRESHOLD) / (1.0 - CONSENSUS_THRESHOLD)
fraction = min(signal_strength, 1.0) * MAX_POSITION_PCT
position_usdc = min(bankroll * fraction, MAX_POSITION_USDC)
```

Examples with $500 bankroll and $10 hard cap:
- 70% → $0 (at threshold, minimum signal)
- 80% → ~$16.50 → Capped at **$10**
- 90% → ~$33.00 → Capped at **$10**
- 97%+ → ~$45.00 → Capped at **$10**

---

## Critical bugs discovered and fixed (do NOT revert)

### 1. Gamma API ignores all filter parameters
**Bug**: `GET /markets?condition_id=X` and `GET /markets?conditionId=X` both ignore the parameter and return the same random 20 markets (always "Russia-Ukraine Ceasefire" as `markets[0]`). All markets showed identical 53.5% mid-price.

**Fix**: Price fetching now uses CLOB API:
- `GET https://clob.polymarket.com/midpoint?token_id=<YES_TOKEN>` → `{"mid": "0.74"}`
- `GET https://clob.polymarket.com/spread?token_id=<YES_TOKEN>` → `{"spread": "0.06"}`

These are public endpoints (no auth required) and correctly return per-market data.

### 2. Polymarket URLs used market slug instead of event slug
**Bug**: `https://polymarket.com/event/{market_slug}` returns "Oops...we didn't forecast this" 404.

**Fix**: Use `events[0]["slug"]` from the Gamma API response:
```python
event_slug = events[0].get("slug", "") if events else ""
url = f"[https://polymarket.com/event/](https://polymarket.com/event/){event_slug}" if event_slug else f"[https://polymarket.com/event/](https://polymarket.com/event/){slug}"
```

### 3. WebSocket VWAP was useless as primary signal
**Bug**: WebSocket `price_change` events are ORDER BOOK UPDATES (bids at $0.01), not completed trades. VWAP of these was always ~1%.

**Fix**: WebSocket ticks now used ONLY for Focus Ratio calculation. CLOB mid-price is the primary signal.

### 4. MIN_TICKS filter was blocking all markets
**Bug**: After removing ticks as primary signal, MIN_TICKS=1 was blocking every market with low CLOB activity (most new markets).

**Fix**: Removed the MIN_TICKS liveness check entirely from `node_analyst` and codebase. Ticks are still collected and used for FR.

### 5. Bot bought already-resolved markets at 100%
**Bug**: CLOB returns `mid=1.0` for resolved markets. Bot saw "100% consensus → BUY" and bet $50 for $0.02 potential profit.

**Fix**: Added filter in `node_analyst` — skip if `consensus >= 0.97`.

### 6. Duplicate positions after container restart
**Bug**: `_seen_condition_ids` is in-memory and resets on restart. Same market got re-observed and re-bet.

**Fix**: Added check in `node_risk_guard` — skip if condition_id already in `open_positions`.

### 7. Dashboard resolution checker used broken Gamma API filter
**Bug**: `_check_resolutions()` was checking `GET /markets?condition_id=X` to detect resolved markets — which returns random markets.

**Fix**: Now uses CLOB `/midpoint`:
- `mid >= 0.99` → WON
- `mid <= 0.01` → LOST
- `{"error": "No orderbook exists"}` → market closed, check `/prices-history` for last price

---

## Dual-token observation

Both YES and NO tokens are observed simultaneously. After the window:
- `yes_mid = CLOB_midpoint(token_ids[0])`
- `no_mid = 1 - yes_mid` (binary market complementarity)
- Pick whichever side has higher probability as `consensus`
- Outcomes label: `outcomes[0]` for YES, `outcomes[1]` for NO

---

## Filter pipeline (node_analyst)

1. CLOB mid-price fetch — if no quotes → SKIP
2. `consensus >= 0.97` → SKIP (already resolved)
3. `spread > MAX_SPREAD (15%)` → SKIP (wide spread)
4. `focus_ratio > 500` → SKIP (HFT noise)
5. If `initial_mid < 0.70` AND `price_move < 3%` → SKIP (no conviction)
6. Multi-market correlation: if related market in same event has consensus ≥ 60%, lower threshold from 70% to 60%
7. `consensus >= effective_threshold` → BUY, else SKIP

---

## Risk guard checks (node_risk_guard)

1. Kill switch active → BLOCKED
2. Already have open position on this condition_id → SKIP
3. Already have >= 3 open positions on this event_id → SKIP (correlation limit/overexposure guard)
4. `liquidity < MIN_TRADE_LIQUIDITY_USD ($500)` → SKIP
5. `position_size_usdc < MIN_POSITION_USDC ($2)` → SKIP

---

## DRY RUN simulation — full cycle

1. **BUY triggered** → `place_fok_order` records sim_trade with `result=OPEN`
2. **Every 5 min** → `redeemer_loop` checks for resolution
3. **Every 2 min** → dashboard `_check_resolutions` polls CLOB midpoint per token:
   - `mid=1.0` → WON, `pnl = contracts - size_usdc`
   - `mid=0.0` or no orderbook → LOST, `pnl = -size_usdc`
4. **Every 60s** → `profit_monitor_loop` checks if current price ≥ 2× entry → SELL FOK
5. P&L chart updates automatically when resolved trade count changes

**Note**: Markets show OPEN until the UMA oracle confirms outcome (12–48h after game ends). "Closes = Apr 18 ended" means the game was Apr 18; OPEN means oracle hasn't resolved yet. This is normal.

---

## Data stored per sim_trade

```json
{
  "timestamp": 1745000000.0,
  "market_question": "SF Giants vs Washington Nationals: Moneyline",
  "condition_id": "0xabc...",
  "market_url": "[https://polymarket.com/event/sf-giants-vs-nationals](https://polymarket.com/event/sf-giants-vs-nationals)",
  "event_id": "0xdef...",
  "end_date": "2026-04-19T01:00:00Z",
  "picked_outcome": "Washington Nationals",
  "token_id": "123456...",
  "side": "BUY",
  "price": 0.745,
  "size_usdc": 7.50,
  "contracts": 10.067,
  "potential_profit": 2.57,
  "result": "OPEN",
  "pnl": null,
  "mode": "DRY_RUN"
}
```

---

## Order execution (LIVE mode)

- Uses Gnosis Safe (`SAFE_ADDRESS`) as treasury — holds USDC
- `PRIVATE_KEY` is a limited-scope session key (NOT the Safe owner key)
- Relayer (Polymarket) pays MATIC gas — bot only needs USDC in Safe
- All orders are FOK (Fill-or-Kill): full fill at target price or cancelled

---

## Running

```bash
# First time setup
cp .env.example .env
# Edit .env with your credentials

# Start everything
docker compose up -d

# Logs
docker logs -f polymarket-python

# Dashboard
open http://localhost:5000

# Clear Redis data (trades, decisions, positions)
docker exec polymarket-redis redis-cli DEL open_positions sim_trades sim_trades:timeline
docker exec polymarket-redis redis-cli KEYS "dec:*" | xargs docker exec -i polymarket-redis redis-cli DEL
docker exec polymarket-redis redis-cli DEL decisions:timeline

# Debug: manually watch a token
docker exec -it polymarket-python python tools/observer.py <TOKEN_ID>
```

---

## Status: what works

| Feature | Status |
|---|---|
| Market discovery (Gamma API, tag_id=1) | ✅ Ready |
| WebSocket observation (dual-token) | ✅ Ready |
| CLOB mid-price signal | ✅ Ready |
| Focus Ratio filter | ✅ Ready |
| Price movement filter | ✅ Ready |
| Multi-market correlation boost | ✅ Ready |
| Scaled-fraction position sizing | ✅ Ready |
| Kill switch / risk manager | ✅ Ready |
| Duplicate position guard | ✅ Ready |
| Maximum Event Correlation (Max 3 bets/event) | ✅ Ready |
| Auto take-profit (2×) | ✅ Ready |
| DRY RUN paper trading + P&L tracking | ✅ Ready |
| Dashboard (port 5000) | ✅ Ready |
| Decision log (Redis + /app/logs/decisions.jsonl) | ✅ Ready |
| Order placement (LIVE) | ⏳ DRY RUN until credentials set in .env |
| Auto-redeem (LIVE) | ⏳ Partial |

---

## What the user still needs to do (external setup)

1. **Gnosis Safe**: create on Polygon at app.safe.global, deposit USDC
2. **Polymarket API keys**: generate at docs.polymarket.com/#authentication
3. **Session key wallet**: create a fresh wallet, add as Safe signer
4. **Fill .env**: set `PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, `SAFE_ADDRESS`, `BANKROLL_USDC`

---

## Key APIs

| API | URL | Notes |
|---|---|---|
| Gamma markets | `https://gamma-api.polymarket.com/markets` | Use for bulk scanning only — individual market filters broken |
| CLOB midpoint | `https://clob.polymarket.com/midpoint?token_id=X` | Per-market price, no auth needed |
| CLOB spread | `https://clob.polymarket.com/spread?token_id=X` | Per-market spread, no auth needed |
| CLOB prices history | `https://clob.polymarket.com/prices-history?market=X&interval=1d&fidelity=1` | Last known price for closed markets |
| CLOB order book | `https://clob.polymarket.com/book?token_id=X` | Full L2 book |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Subscribe: `{"assets_ids": [token_id,...], "type": "market"}` |

---

## Dashboard features (port 5000)

- Modern Dark Glassmorphism UI (Inter & JetBrains Mono typography) with SVG robot favicon.
- Expanded view areas (350px) for charts and active scan lists.
- Stats cards: total P&L, win rate, trades, daily P&L, active observations.
- Cumulative P&L chart (updates when trades resolve).
- Active Observations panel (scrollable, live countdown bars, tick counter).
- Trade History table: Placed, Market, Link, **Bet on** (outcome), Closes, Entry %, Bet $, Potential, Status, P&L.
- Decision Log: every BUY/SKIP with mid, spread, ticks, FR, Size%, reason (Kelly logic successfully retired).
- DRY RUN / LIVE mode toggle.
- Kill switch banner.

---

## Known limitations / future work

- **Wide spread markets**: Basketball O/U and esports markets often have 20–100% spreads → all skipped correctly.
- **Near 50/50 markets**: Most new markets open near 50/50, no signal. Strategy works when one side is heavily favored at open.
- **Esports markets**: Total Kills props have zero CLOB activity but correct prices. They pass if price > 70%.
- **Oracle delay**: Polymarket takes 12–48h to resolve after games end. Trades stay OPEN during this window.
- **Bankroll is static**: Currently uses fixed `BANKROLL_USDC` from config. Could be made dynamic (fetch Safe balance).