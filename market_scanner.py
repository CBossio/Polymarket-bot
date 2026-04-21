import json
import logging
import time
import requests
from datetime import datetime, timezone, timedelta
from config import GAMMA_API_BASE, SPORTS_TAG_IDS, MIN_LIQUIDITY_USD, MAX_MARKET_AGE_HOURS, MAX_HOURS_TO_EVENT, MIN_HOURS_TO_EVENT

logger = logging.getLogger(__name__)

# Persisted in Redis; also kept in-memory for speed
_seen_condition_ids: set = set()


def _is_recently_created(market: dict) -> bool:
    """Returns True only if the market was created within MAX_MARKET_AGE_HOURS."""
    created_str = market.get("createdAt", "")
    if not created_str:
        return False
    try:
        created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - created_at
        return age <= timedelta(hours=MAX_MARKET_AGE_HOURS)
    except ValueError:
        return False


def _parse_game_start(market: dict):
    """Returns game start as aware datetime, or None if unavailable."""
    for field in ("gameStartTime", "startDate"):
        val = market.get(field, "")
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            try:
                return datetime.fromtimestamp(float(val), tz=timezone.utc)
            except (ValueError, TypeError):
                continue
    return None


def _is_event_soon(market: dict) -> tuple[bool, str]:
    """
    Returns (should_observe, label).
    Skips markets where the game starts more than MAX_HOURS_TO_EVENT hours from now.
    Markets with no game time are always observed (e.g. futures/outrights).
    """
    game_start = _parse_game_start(market)
    if game_start is None:
        return True, "no-start-time"
    now = datetime.now(timezone.utc)
    hours_away = (game_start - now).total_seconds() / 3600
    if hours_away < MIN_HOURS_TO_EVENT:
        return False, f"starts too soon ({hours_away:.1f}h)"
    if hours_away > MAX_HOURS_TO_EVENT:
        return False, f"{hours_away:.0f}h to event"
    return True, f"{hours_away:.1f}h to event"


def scan_for_sports_markets() -> list:
    """
    Polls Gamma API for NEWLY CREATED sports markets (within MAX_MARKET_AGE_HOURS).

    Core premise of the Delayed Mirror strategy: smart money rushes in during the
    first hours of a new market to correct mispriced odds. After that, the market
    is already fairly priced — no edge to follow.
    """
    new_markets = []

    for tag_id in SPORTS_TAG_IDS:
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_id": tag_id,
                    "limit": 500,
                    "order": "createdAt",   # Newest markets first
                    "ascending": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()

            for m in markets:
                condition_id = m.get("conditionId", "")
                if not condition_id or condition_id in _seen_condition_ids:
                    continue

                # Core filter: only newly created markets
                if not _is_recently_created(m):
                    logger.debug(
                        f"[Scanner] Skipping old market: {m.get('question','')[:50]} "
                        f"(created {m.get('createdAt','?')[:10]})"
                    )
                    continue

                # Only process markets with a live CLOB order book
                if not m.get("enableOrderBook", False):
                    continue
                if not m.get("acceptingOrders", False):
                    continue

                # Skip markets where the game is too far away
                should_obs, time_label = _is_event_soon(m)
                if not should_obs:
                    logger.debug(f"[Scanner] Too early ({time_label}): {m.get('question','')[:50]}")
                    continue

                liquidity = float(m.get("liquidityClob", m.get("liquidity", 0)))
                if liquidity < MIN_LIQUIDITY_USD:
                    continue

                token_ids = m.get("clobTokenIds", [])
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)

                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                if not token_ids:
                    continue

                # Parse market age for display
                created_str = m.get("createdAt", "")
                try:
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
                    age_label = f"{age_hours:.1f}h old"
                except Exception:
                    age_label = "?"

                _seen_condition_ids.add(condition_id)
                slug = m.get("slug", "")
                events = m.get("events") or []
                event_id = str(events[0].get("id", "")) if events else ""
                event_slug = events[0].get("slug", "") if events else ""
                url = f"https://polymarket.com/event/{event_slug}" if event_slug else (
                      f"https://polymarket.com/event/{slug}" if slug else "")
                game_start = _parse_game_start(m)
                market_data = {
                    "condition_id": condition_id,
                    "question": m.get("question", "Unknown"),
                    "slug": slug,
                    "url": url,
                    "created_at": m.get("createdAt", ""),
                    "start_date": m.get("startDateIso", ""),
                    "end_date": m.get("endDateIso", ""),
                    "token_id": str(token_ids[0]),
                    "token_ids": [str(t) for t in token_ids],
                    "outcomes": outcomes,
                    "liquidity": liquidity,
                    "volume": float(m.get("volumeClob", m.get("volume", 0))),
                    "best_bid": float(m.get("bestBid", 0) or 0),
                    "best_ask": float(m.get("bestAsk", 0) or 0),
                    "event_id": event_id,
                    "game_start_time": game_start.isoformat() if game_start else "",
                    "hours_to_event": time_label,
                    "tag_id": tag_id,
                }
                new_markets.append(market_data)
                logger.info(
                    f"[Scanner] NEW: {m.get('question', '')[:50]} | "
                    f"${liquidity:,.0f} liq | {age_label} | {time_label}"
                )

        except requests.RequestException as e:
            logger.error(f"[Scanner] HTTP error for tag {tag_id}: {e}")
        except Exception as e:
            logger.error(f"[Scanner] Unexpected error for tag {tag_id}: {e}")

    return new_markets


def get_market_by_token(token_id: str) -> dict:
    """Resolves a token ID to its market metadata (used for display / debugging)."""
    try:
        resp = requests.get(
            f"{GAMMA_API_BASE}/markets",
            params={"clobTokenIds": token_id},
            timeout=10,
        )
        markets = resp.json()
        if markets:
            m = markets[0]
            token_ids = m.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            outcome_label = ""
            if str(token_id) in token_ids:
                idx = token_ids.index(str(token_id))
                outcome_label = outcomes[idx] if idx < len(outcomes) else ""
            return {
                "question": m.get("question", ""),
                "outcome": outcome_label,
                "condition_id": m.get("conditionId", ""),
                "liquidity": float(m.get("liquidity", 0)),
            }
    except Exception as e:
        logger.warning(f"[Scanner] Could not resolve token {token_id[:12]}: {e}")
    return {}
