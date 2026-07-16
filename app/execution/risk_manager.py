"""
risk_manager.py

당일 리스크 상태(연속 손절 횟수, 당일 손익률, 매매 횟수)를 추적한다.
policy_selector가 신규매수 차단 여부를 판단하는 데 사용한다.

상태 파일: data/state/risk_state_YYYYMMDD.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.utils.data_paths import STATE_DIR as _STATE_DIR

_ROOT = Path(__file__).resolve().parent.parent.parent


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


class RiskManager:
    def __init__(self, cfg=None, starting_capital: float = 10_000_000, date_str: str = None):
        self.cfg = cfg
        self.date_str = date_str or _today()
        self.starting_capital = starting_capital
        self._state = self._load()

    # ------------------------------------------------------------------
    def _path(self) -> Path:
        return _STATE_DIR / f"risk_state_{self.date_str}.json"

    def _load(self) -> dict:
        path = self._path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("[RiskManager] 상태 로드 실패: %s", exc)
        return {
            "date": self.date_str,
            "trade_count": 0,
            "consecutive_losses": 0,
            "daily_realized_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "starting_capital": self.starting_capital,
            "trades": [],
        }

    def _save(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("[RiskManager] 상태 저장 실패: %s", exc)

    # ------------------------------------------------------------------
    def get_state(self) -> dict:
        return {
            "consecutive_losses": self._state.get("consecutive_losses", 0),
            "daily_pnl_pct": self._state.get("daily_pnl_pct", 0.0),
            "trade_count": self._state.get("trade_count", 0),
        }

    def record_trade_result(
        self,
        symbol: str,
        pnl_amount: float,
        pnl_pct: float,
        was_stop_loss: bool,
        side: str = "sell",
    ) -> None:
        self._state["trade_count"] = self._state.get("trade_count", 0) + 1
        self._state["daily_realized_pnl"] = self._state.get("daily_realized_pnl", 0.0) + pnl_amount

        capital = self._state.get("starting_capital", self.starting_capital) or 1.0
        self._state["daily_pnl_pct"] = round(self._state["daily_realized_pnl"] / capital * 100, 3)

        if was_stop_loss:
            self._state["consecutive_losses"] = self._state.get("consecutive_losses", 0) + 1
        else:
            self._state["consecutive_losses"] = 0

        self._state.setdefault("trades", []).append({
            "symbol": symbol, "side": side, "pnl_amount": pnl_amount, "pnl_pct": pnl_pct,
            "was_stop_loss": was_stop_loss,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })
        self._save()
        logger.info(
            "[RiskManager] 거래 기록: %s pnl=%.0f(%.2f%%) stop_loss=%s → 연속손절=%d 당일손익률=%.2f%%",
            symbol, pnl_amount, pnl_pct, was_stop_loss,
            self._state["consecutive_losses"], self._state["daily_pnl_pct"],
        )

    def can_open_new_position(
        self,
        current_position_count: int,
        max_positions: int = 3,
        max_daily_trades: int = 3,
    ) -> tuple[bool, str]:
        if current_position_count >= max_positions:
            return False, f"최대 보유종목수 도달({current_position_count}/{max_positions})"
        if self._state.get("trade_count", 0) >= max_daily_trades:
            return False, f"당일 최대 매매횟수 도달({self._state.get('trade_count', 0)}/{max_daily_trades})"
        return True, ""
