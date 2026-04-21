import logging
import time
import requests
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from redis_manager import RedisManager
from risk_manager import RiskManager
from order_executor import OrderExecutor
from config import (
    CONSENSUS_THRESHOLD, FOCUS_RATIO_NOISE_THRESHOLD,
    MAX_POSITION_PCT, MIN_POSITION_USDC,
    MIN_TRADE_LIQUIDITY_USD, MAX_SPREAD, CLOB_API_BASE, 
    MAX_POSITION_USDC,
    MIN_PRICE_MOVE, CORR_BOOST_THRESHOLD,
)

logger = logging.getLogger(__name__)

redis_mgr = RedisManager()
risk_mgr = RiskManager()
executor = OrderExecutor()


# --- State Schema ---

class GraphState(TypedDict):
    token_id: str           # Chosen token (updated by analyst to the favored side)
    token_ids: list         # All available tokens [YES, NO]
    outcomes: list          # Outcome labels e.g. ["Giants", "Nationals"] or ["Yes", "No"]
    condition_id: str
    event_id: str           # Polymarket event ID (groups related markets)
    market_question: str
    market_url: str
    market_end_date: str    # ISO date when market closes (for display)
    picked_outcome: str     # Which outcome we bet on (e.g. "Nationals -1.5")
    liquidity: float
    bankroll: float
    initial_mid: float      # Mid-price at observation START (for movement filter)
    ticks_data: list
    ticks_by_token: dict
    avg_probability: float  # Current market mid-price
    spread: float
    price_move: float       # How much mid moved during observation window
    focus_ratio: float
    size_fraction: float
    position_size_usdc: float
    decision: str
    reason: str
    order_result: Optional[dict]


# --- Node 1: Scout — loads WebSocket ticks (liveness + FR check) ---

def node_scout(state: GraphState) -> dict:
    token_ids = state.get("token_ids") or [state["token_id"]]
    ticks_by_token = {}
    for tid in token_ids[:2]:
        ticks = redis_mgr.get_recent_ticks(tid, count=200)
        ticks_by_token[tid] = ticks
        logger.info(f"[Scout] {tid[:16]}… → {len(ticks)} ticks")
    return {"ticks_by_token": ticks_by_token}


# --- Helper: fetch current mid-price from CLOB API ---
# NOTE: Gamma API ignores condition_id/conditionId filter params and always returns
# the same default 20 markets. CLOB /midpoint and /spread endpoints work correctly
# with a specific token_id and require no authentication.

def _get_market_prices(yes_token_id: str) -> dict:
    """
    Returns spread and mid-price for the YES token via CLOB API.
    NO token mid = 1 - YES mid (binary market complementarity).
    """
    try:
        mid_r = requests.get(
            f"{CLOB_API_BASE}/midpoint",
            params={"token_id": yes_token_id},
            timeout=8,
        )
        mid_r.raise_for_status()
        mid = float(mid_r.json().get("mid", 0) or 0)
        if mid <= 0:
            return {"bid": 0.0, "ask": 0.0, "mid": 0.0, "spread": 1.0, "ok": False}

        spr_r = requests.get(
            f"{CLOB_API_BASE}/spread",
            params={"token_id": yes_token_id},
            timeout=8,
        )
        spr_r.raise_for_status()
        spread = float(spr_r.json().get("spread", 0) or 0)

        bid = round(mid - spread / 2, 4)
        ask = round(mid + spread / 2, 4)
        return {"bid": bid, "ask": ask, "mid": mid, "spread": spread, "ok": True}
    except Exception as e:
        logger.warning(f"[Analyst] CLOB API error for {yes_token_id[:16]}: {e}")
    return {"bid": 0.0, "ask": 0.0, "mid": 0.0, "spread": 1.0, "ok": False}


# --- Node 2: Analyst — mid-price is the primary signal ---
#
# Strategy change: WebSocket VWAP was unreliable because price_change events are
# order book updates (bids at $0.01), NOT completed trades. Smart money conviction
# is better captured by the Gamma API bid-ask mid-price, which reflects where the
# actual market is pricing the outcome.
#
# WebSocket ticks are now used only as a liveness check (is anyone watching this market?)
# and for Focus Ratio (is it all bots?).

def node_analyst(state: GraphState) -> dict:
    ticks_by_token = state.get("ticks_by_token", {})
    token_ids = state.get("token_ids") or [state["token_id"]]
    yes_token = str(token_ids[0])
    no_token = str(token_ids[1]) if len(token_ids) > 1 else None

    # --- Primary signal: CLOB API mid-price for the YES token ---
    prices = _get_market_prices(yes_token)

    if not prices["ok"]:
        return {
            "decision": "SKIP",
            "reason": "No quotes — market has no active bid/ask",
            "avg_probability": 0.0,
            "spread": 1.0,
            "focus_ratio": 0.0,
            "price_move": 0.0,
        }

    yes_mid = prices["mid"]
    no_mid = 1.0 - yes_mid
    spread = prices["spread"]

    # Pick the favored side (whichever has the higher probability)
    initial_mid = state.get("initial_mid", 0.0)
    outcomes = state.get("outcomes", [])
    if yes_mid >= no_mid:
        chosen_token = yes_token
        consensus = yes_mid
        side_label = "YES"
        initial_consensus = initial_mid
        picked_outcome = outcomes[0] if outcomes else "YES"
    else:
        chosen_token = no_token or yes_token
        consensus = no_mid
        side_label = "NO"
        initial_consensus = (1.0 - initial_mid) if initial_mid > 0 else 0.0
        picked_outcome = outcomes[1] if len(outcomes) > 1 else "NO"

    price_move = round(consensus - initial_consensus, 4)

    chosen_ticks = ticks_by_token.get(chosen_token, [])
    all_ticks = sum(len(t) for t in ticks_by_token.values())

    # Focus ratio on the chosen token (bot noise filter)
    total_tx = len(chosen_ticks)
    unique_wallets = max(redis_mgr.get_wallet_count(chosen_token), 1)
    focus_ratio = total_tx / unique_wallets if total_tx > 0 else 0

    logger.info(
        f"[Analyst] YES={yes_mid:.1%} NO={no_mid:.1%} spread={spread:.3f} | "
        f"chosen={side_label} ({chosen_token[:14]}…) consensus={consensus:.1%} move={price_move:+.1%} | "
        f"ticks={all_ticks} FR={focus_ratio:.1f}"
    )

    base = {
        "token_id": chosen_token,
        "picked_outcome": picked_outcome,
        "ticks_data": chosen_ticks,
        "avg_probability": round(consensus, 4),
        "spread": round(spread, 4),
        "focus_ratio": round(focus_ratio, 1),
        "price_move": price_move,
    }

    # Already resolved or no upside left — CLOB returns ~1.0 for resolved markets
    if consensus >= 0.97:
        return {**base, "decision": "SKIP",
                "reason": f"Price {consensus:.0%} — market likely already resolved, no upside"}

    # Wide spread = market makers unsure, risk of bad fill
    if spread > MAX_SPREAD:
        return {**base, "decision": "SKIP",
                "reason": f"Wide spread ({spread:.0%}) — market too uncertain to follow"}

    # Noise filter
    if focus_ratio > FOCUS_RATIO_NOISE_THRESHOLD:
        return {**base, "decision": "SKIP",
                "reason": f"Algorithmic noise: FR={focus_ratio:.0f} > {FOCUS_RATIO_NOISE_THRESHOLD}"}

    # Multi-market correlation: lower threshold if a related market confirms the signal
    event_id = state.get("event_id", "")
    if event_id:
        redis_mgr.store_event_signal(event_id, chosen_token, consensus, side_label)
        other_signals = [s for s in redis_mgr.get_event_signals(event_id) if s["token_id"] != chosen_token]
        correlated = any(s["consensus"] >= CORR_BOOST_THRESHOLD for s in other_signals)
    else:
        correlated = False
    effective_threshold = max(0.55, CONSENSUS_THRESHOLD - 0.10) if correlated else CONSENSUS_THRESHOLD

    # Price movement filter: market started below effective_threshold but didn't move enough
    if initial_consensus > 0 and initial_consensus < effective_threshold:
        if price_move < MIN_PRICE_MOVE:
            return {**base, "decision": "SKIP",
                    "reason": f"Price barely moved ({price_move:+.1%}) — no smart money conviction during window"}

    # Main signal
    if consensus >= effective_threshold:
        boost_note = f" [corr-boost {CONSENSUS_THRESHOLD:.0%}→{effective_threshold:.0%}]" if correlated else ""
        return {**base, "decision": "BUY",
                "reason": f"Consensus {consensus:.1%} on {side_label} (spread={spread:.2%}, ticks={all_ticks}){boost_note}"}

    return {**base, "decision": "SKIP",
            "reason": f"Weak consensus: {consensus:.1%} < {effective_threshold:.0%}"}


# --- Node 3: Position Sizer (scaled fraction) ---
# Kelly requires an independent probability estimate separate from the market price.
# Since we use the market price as our signal, Kelly always yields ~0 edge.
# Instead: scale linearly from 0% at CONSENSUS_THRESHOLD to MAX_POSITION_PCT at 100%.

def node_sizer(state: GraphState) -> dict:
    if state.get("decision") != "BUY":
        return {"size_fraction": 0.0, "position_size_usdc": 0.0}

    p = state["avg_probability"]
    signal_strength = (p - CONSENSUS_THRESHOLD) / (1.0 - CONSENSUS_THRESHOLD)
    fraction = min(signal_strength, 1.0) * MAX_POSITION_PCT
    position_usdc = min(state.get("bankroll", 0.0) * fraction, MAX_POSITION_USDC)

    logger.info(
        f"[Sizer] consensus={p:.1%} strength={signal_strength:.2f} "
        f"fraction={fraction:.3f} position=${position_usdc:.2f}"
    )
    return {"size_fraction": fraction, "position_size_usdc": position_usdc}


# --- Node 4: Risk Guard ---

def node_risk_guard(state: GraphState) -> dict:
    if state.get("decision") != "BUY":
        return {}

    if risk_mgr.is_kill_switch_active():
        return {"decision": "BLOCKED", "reason": "Kill switch active — daily drawdown limit exceeded"}

    open_positions = redis_mgr.get_open_positions()
    open_cids = {p["condition_id"] for p in open_positions}
    if state.get("condition_id") in open_cids:
        return {"decision": "SKIP", "reason": "Already have open position on this market"}

    event_id = state.get("event_id")
    if event_id:
        event_bets = sum(1 for p in open_positions if p.get("event_id") == event_id)
        if event_bets >= 3:
            return {"decision": "SKIP", "reason": f"Already have {event_bets} open positions on this event (max correlation limit)"}

    if state.get("liquidity", 0) < MIN_TRADE_LIQUIDITY_USD:
        return {
            "decision": "SKIP",
            "reason": f"Liquidity ${state.get('liquidity', 0):,.0f} < min ${MIN_TRADE_LIQUIDITY_USD:,.0f} to trade safely",
        }

    if state.get("position_size_usdc", 0) < MIN_POSITION_USDC:
        return {
            "decision": "SKIP",
            "reason": f"Position ${state.get('position_size_usdc', 0):.2f} < min ${MIN_POSITION_USDC}",
        }

    return {}


# --- Node 5: Executor ---

def node_executor(state: GraphState) -> dict:
    decision = state.get("decision")
    reason = state.get("reason", "")
    result = None

    if decision != "BUY":
        logger.info(f"[Executor] No trade — {decision}: {reason}")
    else:
        token = state["token_id"]
        price = state["avg_probability"]
        size = state["position_size_usdc"]

        result = executor.place_fok_order(
            token_id=token,
            price=price,
            size_usdc=size,
            side="BUY",
            market_meta={
                "question": state.get("market_question", ""),
                "condition_id": state.get("condition_id", ""),
                "url": state.get("market_url", ""),
                "end_date": state.get("market_end_date", ""),
                "picked_outcome": state.get("picked_outcome", ""),
            }
        )

        if result.get("success"):
            risk_mgr.record_trade(token, size, price)
            redis_mgr.mark_market_bet_placed(
                condition_id=state["condition_id"],
                token_id=token,
                size_usdc=size,
                price=price,
                contracts=round(size / price, 4) if price > 0 else 0,
                event_id=state.get("event_id", "")
            )
            logger.info(f"[Executor] Trade placed: ${size:.2f} @ {price:.3f}")
        else:
            logger.warning(f"[Executor] Trade failed: {result.get('error')}")

    redis_mgr.log_decision({
        "timestamp": time.time(),
        "market": state.get("market_question", ""),
        "market_url": state.get("market_url", ""),
        "condition_id": state.get("condition_id", ""),
        "token_id": state.get("token_id", ""),
        "decision": decision or "SKIP",
        "reason": reason,
        "vwap": round(state.get("avg_probability", 0), 4),
        "spread": round(state.get("spread", 0), 4),
        "focus_ratio": round(state.get("focus_ratio", 0), 1),
        "fraction_pct": round(state.get("size_fraction", 0) * 100, 2),
        "position_usdc": round(state.get("position_size_usdc", 0), 2),
        "ticks": len(state.get("ticks_data", [])),
        "liquidity": state.get("liquidity", 0),
    })

    return {"order_result": result}


# --- Graph ---

def _build_graph():
    workflow = StateGraph(GraphState)
    workflow.add_node("scout", node_scout)
    workflow.add_node("analyst", node_analyst)
    workflow.add_node("sizer", node_sizer)
    workflow.add_node("risk_guard", node_risk_guard)
    workflow.add_node("executor", node_executor)
    workflow.set_entry_point("scout")
    workflow.add_edge("scout", "analyst")
    workflow.add_edge("analyst", "sizer")
    workflow.add_edge("sizer", "risk_guard")
    workflow.add_edge("risk_guard", "executor")
    workflow.add_edge("executor", END)
    return workflow.compile()


_brain = _build_graph()


def run_brain_for_market(market: dict, bankroll: float) -> dict:
    token_ids = market.get("token_ids", [market["token_id"]])
    initial = {
        "token_id": str(token_ids[0]),
        "token_ids": [str(t) for t in token_ids[:2]],
        "outcomes": market.get("outcomes", []),
        "condition_id": market.get("condition_id", ""),
        "event_id": market.get("event_id", ""),
        "market_question": market.get("question", ""),
        "market_url": market.get("url", ""),
        "market_end_date": market.get("end_date", ""),
        "liquidity": market.get("liquidity", 0.0),
        "bankroll": bankroll,
        "initial_mid": market.get("initial_mid", 0.0),
        "ticks_data": [],
        "ticks_by_token": {},
        "avg_probability": 0.0,
        "spread": 1.0,
        "price_move": 0.0,
        "focus_ratio": 0.0,
        "size_fraction": 0.0,
        "position_size_usdc": 0.0,
        "picked_outcome": "",
        "decision": "PENDING",
        "reason": "",
        "order_result": None,
    }
    return _brain.invoke(initial)
