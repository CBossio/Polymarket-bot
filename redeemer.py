import logging
import requests
from config import GAMMA_API_BASE

logger = logging.getLogger(__name__)


class Redeemer:
    """
    Checks for resolved markets and redeems winning positions.

    Polymarket does NOT auto-pay winners after UMA oracle resolution.
    The bot must call redeem explicitly, otherwise USDC sits locked in the CTF contract.

    Resolution flow:
      1. UMA oracle resolves the market → conditionId gets a winning outcome
      2. Gamma API reflects resolved=True + winner
      3. We call py-clob-client redeem (or directly call CTF contract via web3.py)
    """

    def __init__(self, executor):
        self.executor = executor

    def check_and_redeem(self, condition_id: str, token_id: str = None) -> dict:
        resolution = self._get_resolution(condition_id)

        if not resolution["resolved"]:
            return {"redeemed": False, "reason": "not_resolved_yet"}

        winner = resolution.get("winning_outcome")
        logger.info(
            f"[Redeemer] Market resolved | condition={condition_id[:12]} | winner={winner}"
        )

        return self._redeem(condition_id, token_id, winner)

    def _get_resolution(self, condition_id: str) -> dict:
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                m = data[0]
                return {
                    "resolved": bool(m.get("resolved", False)),
                    "winning_outcome": m.get("winner"),
                }
        except Exception as e:
            logger.error(f"[Redeemer] Resolution check failed for {condition_id[:12]}: {e}")
        return {"resolved": False, "winning_outcome": None}

    def _redeem(self, condition_id: str, token_id: str, winner: str) -> dict:
        if self.executor.dry_run:
            logger.info(
                f"[DRY RUN] Would redeem winning position | "
                f"condition={condition_id[:12]} | winner={winner}"
            )
            return {"redeemed": True, "dry_run": True}

        # py-clob-client exposes redeem_positions for CTF settlements
        # Falls back to logging a manual action if the method isn't available
        client = self.executor.client
        if client is None:
            return {"redeemed": False, "error": "No CLOB client"}

        try:
            result = client.redeem_positions(condition_id)
            logger.info(f"[Redeemer] Redemption submitted: {result}")
            return {"redeemed": True, "result": result}
        except AttributeError:
            # Older py-clob-client versions don't expose redeem_positions.
            # In that case, interact with the CTF contract directly via web3.py.
            logger.warning(
                "[Redeemer] redeem_positions not available in this py-clob-client version. "
                "Manual redemption required via Polymarket UI or direct CTF contract call."
            )
            return {"redeemed": False, "error": "redeem_positions_not_supported"}
        except Exception as e:
            logger.error(f"[Redeemer] Redemption failed: {e}")
            return {"redeemed": False, "error": str(e)}
