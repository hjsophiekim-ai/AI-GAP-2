import math
from datetime import datetime

from app.models import Position
from app.config import get_config
from app.logger import logger


class SellManager:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _first_take_profit_rate(self) -> float:
        return float(self.cfg.trading.get("first_take_profit_rate", 3.0))

    def _second_take_profit_rate(self) -> float:
        return float(self.cfg.trading.get("second_take_profit_rate", 5.0))

    def _stop_loss_rate(self) -> float:
        return float(self.cfg.trading.get("stop_loss_rate", -1.5))

    def _bulk_sell_1150_time(self) -> str:
        return self.cfg.trading.get("bulk_sell_1150_time", "11:50")

    def _force_sell_time(self) -> str:
        return self.cfg.trading.get("force_sell_time", "13:00")

    def _emergency_sell_time(self) -> str:
        return self.cfg.trading.get("emergency_sell_time", "15:10")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_positions(self, positions: list[Position]) -> list[dict]:
        """
        Evaluate each position and determine sell action based on profit/loss rules.

        Priority order (highest first):
          1. profit_rate >= second_take_profit_rate (5%)  -> sell_all
          2. profit_rate >= first_take_profit_rate  (3%)  -> sell_half (or sell_all if qty==1)
          3. profit_rate <= stop_loss_rate          (-1.5%) -> sell_all

        Returns a list of dicts:
          {symbol, name, action, reason, quantity, current_price}
        Only positions with action != "hold" are included (hold entries are omitted
        to keep the list actionable, but hold is still returned for completeness).
        """
        first_tp = self._first_take_profit_rate()
        second_tp = self._second_take_profit_rate()
        stop_loss = self._stop_loss_rate()

        results: list[dict] = []

        for pos in positions:
            rate = pos.profit_rate
            action = "hold"
            reason = ""
            quantity = pos.quantity

            # Check +5% first so a stock that hits 5% gets sell_all, not sell_half
            if rate >= second_tp:
                action = "sell_all"
                reason = f"+{second_tp:.0f}% 잔여전량매도"
                quantity = pos.quantity
            elif rate >= first_tp:
                if pos.quantity == 1:
                    action = "sell_all"
                    reason = f"+{first_tp:.0f}% 전량매도(1주)"
                    quantity = pos.quantity
                else:
                    action = "sell_half"
                    quantity = math.ceil(pos.quantity / 2)
                    reason = f"+{first_tp:.0f}% 절반매도"
            elif rate <= stop_loss:
                action = "sell_all"
                reason = f"{stop_loss:.1f}% 손절"
                quantity = pos.quantity

            entry = {
                "symbol": pos.symbol,
                "name": pos.name,
                "action": action,
                "reason": reason,
                "quantity": quantity,
                "current_price": pos.current_price,
            }
            results.append(entry)

            if action != "hold":
                logger.info(
                    f"[매도판단] {pos.symbol} {pos.name} profit={rate:.2f}% "
                    f"action={action} reason={reason} qty={quantity}"
                )

        return results

    def check_time_exits(
        self,
        positions: list[Position],
        current_time: str = None,
    ) -> list[dict]:
        """
        Check time-based exit conditions.

        current_time format: "HH:MM"
          >= force_sell_time (13:00)    -> sell_all, reason="13:00 시간청산"
          >= emergency_sell_time (15:10) -> sell_all, reason="15:10 비상청산"

        Returns list of dicts with action="sell_all" for triggered positions.
        """
        if current_time is None:
            current_time = datetime.now().strftime("%H:%M")

        bulk_1150_time = self._bulk_sell_1150_time()
        force_time = self._force_sell_time()
        emergency_time = self._emergency_sell_time()

        results: list[dict] = []

        for pos in positions:
            action = "hold"
            reason = ""

            # Priority: emergency(15:10) > force(13:00) > bulk_1150(11:50)
            if current_time >= emergency_time:
                action = "sell_all"
                reason = f"{emergency_time} 비상청산"
            elif current_time >= force_time:
                action = "sell_all"
                reason = f"{force_time} 시간청산"
            elif current_time >= bulk_1150_time:
                action = "sell_all"
                reason = f"{bulk_1150_time} 일괄청산"

            if action != "hold":
                logger.info(
                    f"[시간청산] {pos.symbol} {pos.name} time={current_time} reason={reason}"
                )
                results.append({
                    "symbol": pos.symbol,
                    "name": pos.name,
                    "action": action,
                    "reason": reason,
                    "quantity": pos.quantity,
                    "current_price": pos.current_price,
                })

        return results

    def check_all_exits(
        self,
        positions: list[Position],
        current_time: str = None,
    ) -> list[dict]:
        """
        Combines evaluate_positions and check_time_exits.
        Each symbol appears at most once; time-based exit takes priority
        over profit/loss evaluation when both trigger for the same symbol.
        """
        profit_results = self.evaluate_positions(positions)
        time_results = self.check_time_exits(positions, current_time)

        # Build a map: symbol -> entry, with time exits overwriting profit exits
        merged: dict[str, dict] = {}

        for entry in profit_results:
            if entry["action"] != "hold":
                merged[entry["symbol"]] = entry

        # Time-based exits take priority (overwrite)
        for entry in time_results:
            merged[entry["symbol"]] = entry

        return list(merged.values())
