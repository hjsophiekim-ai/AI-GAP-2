"""
auto_sell_service.py — 자동매도 감시 서비스

보유종목 현재가를 주기적으로 조회하고, 수익률 조건 충족 시 자동 매도 주문 실행.
- +3% 절반매도 (1회, half_sold 기록)
- +5% 전량매도 (1회, all_sold 기록)
- 우선순위: +5% 전량매도 > +3% 절반매도
- 중복 주문 방지: pending_order 플래그 + state 파일 영속 저장
"""

import warnings
warnings.warn(
    "AutoSellService is deprecated. Use IntradayAutoTradeService instead.",
    DeprecationWarning,
    stacklevel=2,
)

import csv
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger


class AutoSellService:
    """자동매도 감시 서비스."""

    DEFAULT_STATE_FILE = "data/state/auto_sell_state.json"
    DEFAULT_LOG_FILE = "data/logs/auto_sell_orders.csv"

    LOG_COLUMNS = [
        "timestamp", "symbol", "name", "avg_buy_price", "current_price",
        "profit_rate", "quantity_before", "sell_quantity", "sell_type",
        "order_type", "order_result", "order_id", "error_message",
    ]

    def __init__(self, kis_client, broker, cfg=None):
        """
        kis_client : KISClient (real mode)
        broker     : KisRealBroker (runtime_real_mode=True で생성 완료)
        cfg        : Config instance (None이면 get_config() 사용)
        """
        from app.config import get_config
        self._cfg = cfg or get_config()
        self._kis = kis_client
        self._broker = broker

        auto_cfg = self._cfg._raw.get("auto_sell", {})

        self._first_tp_rate: float = float(
            os.getenv("AUTO_SELL_FIRST_TP_RATE",
                      auto_cfg.get("first_take_profit_rate", 3.0))
        )
        self._first_tp_ratio: float = float(
            os.getenv("AUTO_SELL_FIRST_TP_RATIO",
                      auto_cfg.get("first_take_profit_sell_ratio", 0.5))
        )
        self._final_tp_rate: float = float(
            os.getenv("AUTO_SELL_FINAL_TP_RATE",
                      auto_cfg.get("final_take_profit_rate", 5.0))
        )
        self._stop_loss_rate: float = float(
            os.getenv("AUTO_SELL_STOP_LOSS_RATE",
                      auto_cfg.get("stop_loss_rate", -2.0))
        )
        self._order_type: str = auto_cfg.get("order_type", "market")
        self._market_start: str = auto_cfg.get("market_start", "09:00")
        self._market_end: str = auto_cfg.get("market_end", "15:20")

        _root = Path(__file__).resolve().parent.parent.parent
        self._state_file: Path = _root / auto_cfg.get("state_file", self.DEFAULT_STATE_FILE)
        self._log_file: Path = _root / auto_cfg.get("log_file", self.DEFAULT_LOG_FILE)

        self.state: dict = {}
        self._last_run_time: Optional[datetime] = None
        self.load_state()

    # ── 상태 파일 ────────────────────────────────────────────────────────

    def load_state(self) -> None:
        """JSON 상태 파일에서 종목별 자동매도 상태 복원."""
        try:
            if self._state_file.exists():
                with open(self._state_file, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
        except Exception as e:
            logger.warning("auto_sell: state 로드 실패 (%s): %s", self._state_file, e)
            self.state = {}

    def save_state(self) -> None:
        """종목별 자동매도 상태를 JSON 파일에 저장."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("auto_sell: state 저장 실패: %s", e)

    # ── 포지션 / 현재가 조회 ─────────────────────────────────────────────

    def load_positions(self) -> list[dict]:
        """KIS 실계좌 보유종목 조회.

        Returns
        -------
        list of dict with keys:
            symbol, name, quantity, avg_buy_price, current_price
        """
        result = self._kis.get_balance()
        if "error" in result:
            raise RuntimeError(f"잔고 조회 실패: {result['error']}")
        positions = []
        for item in (result.get("positions") or []):
            positions.append({
                "symbol": item["symbol"],
                "name": item["name"],
                "quantity": int(item.get("quantity", 0)),
                "avg_buy_price": float(item.get("avg_price", 0)),
                "current_price": float(item.get("current_price", 0)),
            })
        return positions

    def get_current_price(self, symbol: str) -> Optional[float]:
        """KIS 현재가 API 조회. 실패 시 None 반환."""
        try:
            result = self._kis.get_current_price(symbol)
            if result and result.get("current_price", 0) > 0:
                return float(result["current_price"])
        except Exception as e:
            logger.warning("auto_sell: 현재가 조회 실패 %s: %s", symbol, e)
        return None

    # ── 조건 계산 ────────────────────────────────────────────────────────

    @staticmethod
    def calculate_profit_rate(avg_buy_price: float, current_price: float) -> float:
        """수익률 (%). avg_buy_price <= 0 이면 0.0 반환."""
        if avg_buy_price <= 0:
            return 0.0
        return (current_price - avg_buy_price) / avg_buy_price * 100

    def should_sell_half(self, position: dict, state: dict) -> bool:
        """절반매도 조건 충족 여부.

        profit_rate >= first_take_profit_rate AND half_sold is False
        """
        return (
            position.get("profit_rate", 0.0) >= self._first_tp_rate
            and not state.get("half_sold", False)
        )

    def should_sell_all(self, position: dict, state: dict) -> bool:
        """전량매도 조건 충족 여부.

        profit_rate >= final_take_profit_rate AND all_sold is False
        """
        return (
            position.get("profit_rate", 0.0) >= self._final_tp_rate
            and not state.get("all_sold", False)
        )

    def should_stop_loss(self, position: dict, state: dict) -> bool:
        """손절 조건 충족 여부.

        profit_rate <= stop_loss_rate AND stop_loss_executed is False
        stop_loss_rate는 음수 (예: -2.0 = -2% 손실)
        """
        return (
            position.get("profit_rate", 0.0) <= self._stop_loss_rate
            and not state.get("stop_loss_executed", False)
            and not state.get("all_sold", False)
        )

    # ── 매도 실행 ────────────────────────────────────────────────────────

    def execute_half_sell(self, position: dict, current_price: float) -> dict:
        """절반매도 실행.

        매도수량 = floor(보유수량 * first_tp_ratio), 최소 1주.
        보유수량 1주일 때 floor(1 * 0.5) = 0 → 1주 전량매도 처리.
        """
        symbol = position["symbol"]
        name = position["name"]
        total_qty = position["quantity"]
        avg_price = position["avg_buy_price"]
        profit_rate = self.calculate_profit_rate(avg_price, current_price)

        raw_qty = math.floor(total_qty * self._first_tp_ratio)
        sell_qty = max(1, raw_qty) if total_qty > 0 else 0

        if sell_qty < 1:
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=0, sell_type="half",
                order_result="SKIP", error="매도수량 0",
            )

        sym_state = self.state.setdefault(symbol, self._new_state(name, avg_price))
        sym_state["pending_order"] = True
        self.save_state()

        try:
            order = self._broker.sell(
                symbol=symbol, name=name,
                quantity=sell_qty, price=0, order_type=self._order_type,
            )
            if order.success:
                sym_state.update({
                    "half_sold": True,
                    "half_sold_at": datetime.now().isoformat(),
                    "last_order_id": order.order_id,
                    "last_sell_type": "half",
                    "last_error": None,
                })
                logger.info(
                    "auto_sell: 절반매도 성공 %s %d주 order_id=%s", symbol, sell_qty, order.order_id
                )
            else:
                sym_state["last_error"] = order.message
                logger.warning("auto_sell: 절반매도 실패 %s: %s", symbol, order.message)

            sym_state["pending_order"] = False
            self.save_state()

            entry = self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=sell_qty, sell_type="half",
                order_result="SUCCESS" if order.success else "FAIL",
                order_id=order.order_id, error="" if order.success else order.message,
            )
            self._append_log(entry)
            return entry

        except Exception as e:
            sym_state["pending_order"] = False
            sym_state["last_error"] = str(e)
            self.save_state()
            logger.error("auto_sell: 절반매도 예외 %s: %s", symbol, e)
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=sell_qty, sell_type="half",
                order_result="ERROR", error=str(e),
            )

    def execute_full_sell(self, position: dict, current_price: float) -> dict:
        """전량매도 실행. 현재 보유수량 전부 매도."""
        symbol = position["symbol"]
        name = position["name"]
        total_qty = position["quantity"]
        avg_price = position["avg_buy_price"]
        profit_rate = self.calculate_profit_rate(avg_price, current_price)

        if total_qty < 1:
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=0, sell_type="full",
                order_result="SKIP", error="보유수량 0",
            )

        sym_state = self.state.setdefault(symbol, self._new_state(name, avg_price))
        sym_state["pending_order"] = True
        self.save_state()

        try:
            order = self._broker.sell(
                symbol=symbol, name=name,
                quantity=total_qty, price=0, order_type=self._order_type,
            )
            if order.success:
                sym_state.update({
                    "all_sold": True,
                    "all_sold_at": datetime.now().isoformat(),
                    "last_order_id": order.order_id,
                    "last_sell_type": "full",
                    "last_error": None,
                })
                logger.info(
                    "auto_sell: 전량매도 성공 %s %d주 order_id=%s", symbol, total_qty, order.order_id
                )
            else:
                sym_state["last_error"] = order.message
                logger.warning("auto_sell: 전량매도 실패 %s: %s", symbol, order.message)

            sym_state["pending_order"] = False
            self.save_state()

            entry = self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=total_qty, sell_type="full",
                order_result="SUCCESS" if order.success else "FAIL",
                order_id=order.order_id, error="" if order.success else order.message,
            )
            self._append_log(entry)
            return entry

        except Exception as e:
            sym_state["pending_order"] = False
            sym_state["last_error"] = str(e)
            self.save_state()
            logger.error("auto_sell: 전량매도 예외 %s: %s", symbol, e)
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=total_qty, sell_type="full",
                order_result="ERROR", error=str(e),
            )

    def execute_stop_loss(self, position: dict, current_price: float) -> dict:
        """손절매도 실행. 보유수량 전량 시장가 매도."""
        symbol = position["symbol"]
        name = position["name"]
        total_qty = position["quantity"]
        avg_price = position["avg_buy_price"]
        profit_rate = self.calculate_profit_rate(avg_price, current_price)

        if total_qty < 1:
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=0, sell_type="stop_loss",
                order_result="SKIP", error="보유수량 0",
            )

        sym_state = self.state.setdefault(symbol, self._new_state(name, avg_price))
        sym_state["pending_order"] = True
        self.save_state()

        try:
            order = self._broker.sell(
                symbol=symbol, name=name,
                quantity=total_qty, price=0, order_type="market",
            )
            if order.success:
                sym_state.update({
                    "stop_loss_executed": True,
                    "all_sold": True,
                    "all_sold_at": datetime.now().isoformat(),
                    "last_order_id": order.order_id,
                    "last_sell_type": "stop_loss",
                    "last_error": None,
                })
                logger.info(
                    "auto_sell: 손절매도 성공 %s %d주 (%.2f%%) order_id=%s",
                    symbol, total_qty, profit_rate, order.order_id,
                )
            else:
                sym_state["last_error"] = order.message
                logger.warning("auto_sell: 손절매도 실패 %s: %s", symbol, order.message)

            sym_state["pending_order"] = False
            self.save_state()

            entry = self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=total_qty, sell_type="stop_loss",
                order_result="SUCCESS" if order.success else "FAIL",
                order_id=order.order_id, error="" if order.success else order.message,
            )
            self._append_log(entry)
            return entry

        except Exception as e:
            sym_state["pending_order"] = False
            sym_state["last_error"] = str(e)
            self.save_state()
            logger.error("auto_sell: 손절매도 예외 %s: %s", symbol, e)
            return self._make_log_entry(
                symbol=symbol, name=name, avg_price=avg_price,
                current_price=current_price, profit_rate=profit_rate,
                qty_before=total_qty, sell_qty=total_qty, sell_type="stop_loss",
                order_result="ERROR", error=str(e),
            )

    # ── 메인 루프 ────────────────────────────────────────────────────────

    def run_once(self) -> list[dict]:
        """1회 가격 점검 및 조건 충족 종목 자동매도.

        Returns
        -------
        list of log entry dicts for executed (or skipped) orders.
        """
        results: list[dict] = []

        if not self._is_market_hours():
            logger.debug("auto_sell: 장외시간 — 스킵")
            return results

        try:
            positions = self.load_positions()
        except Exception as e:
            logger.error("auto_sell: 포지션 조회 실패: %s", e)
            return results

        now_iso = datetime.now().isoformat()

        for pos in positions:
            symbol = pos["symbol"]
            qty = pos["quantity"]
            avg_price = pos["avg_buy_price"]

            if qty < 1 or avg_price <= 0:
                continue

            # 현재가: KIS API 우선, 실패 시 잔고 응답값 fallback
            current_price = self.get_current_price(symbol) or pos.get("current_price", 0)
            if not current_price or current_price <= 0:
                continue

            # 종목 state 초기화
            if symbol not in self.state:
                self.state[symbol] = self._new_state(pos["name"], avg_price)

            sym_state = self.state[symbol]
            sym_state["name"] = pos["name"]
            sym_state["avg_buy_price"] = avg_price

            # 전량매도 완료 → 스킵
            if sym_state.get("all_sold"):
                continue

            # pending 중복 방지
            if sym_state.get("pending_order"):
                logger.debug("auto_sell: %s pending_order — 스킵", symbol)
                continue

            profit_rate = self.calculate_profit_rate(avg_price, current_price)
            sym_state["last_checked_at"] = now_iso
            sym_state["last_profit_rate"] = round(profit_rate, 2)

            pos_with_rate = {**pos, "current_price": current_price, "profit_rate": profit_rate}

            # 우선순위: 손절 > +final% 전량매도 > +first% 절반매도
            if self.should_stop_loss(pos_with_rate, sym_state):
                result = self.execute_stop_loss(pos_with_rate, current_price)
                results.append(result)
            elif self.should_sell_all(pos_with_rate, sym_state):
                result = self.execute_full_sell(pos_with_rate, current_price)
                results.append(result)
            elif self.should_sell_half(pos_with_rate, sym_state):
                result = self.execute_half_sell(pos_with_rate, current_price)
                results.append(result)

        self.save_state()
        self._last_run_time = datetime.now()
        return results

    def run_once_if_due(self, interval_seconds: int = 10) -> list[dict]:
        """interval_seconds가 경과한 경우에만 run_once() 실행 (rate-limiting)."""
        if self._last_run_time is not None:
            elapsed = (datetime.now() - self._last_run_time).total_seconds()
            if elapsed < interval_seconds:
                return []
        return self.run_once()

    # ── 장 시간 확인 ─────────────────────────────────────────────────────

    def _is_market_hours(self) -> bool:
        now = datetime.now()
        sh, sm = map(int, self._market_start.split(":"))
        eh, em = map(int, self._market_end.split(":"))
        cur = now.hour * 60 + now.minute
        return (sh * 60 + sm) <= cur <= (eh * 60 + em)

    # ── 헬퍼 ────────────────────────────────────────────────────────────

    @staticmethod
    def _new_state(name: str, avg_buy_price: float) -> dict:
        return {
            "name": name,
            "avg_buy_price": avg_buy_price,
            "half_sold": False,
            "all_sold": False,
            "stop_loss_executed": False,
            "half_sold_at": None,
            "all_sold_at": None,
            "last_checked_at": None,
            "last_profit_rate": 0.0,
            "pending_order": False,
            "last_error": None,
            "last_order_id": None,
            "last_sell_type": None,
        }

    def _make_log_entry(
        self, symbol, name, avg_price, current_price, profit_rate,
        qty_before, sell_qty, sell_type, order_result,
        order_id: str = "", error: str = "",
    ) -> dict:
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": name,
            "avg_buy_price": avg_price,
            "current_price": current_price,
            "profit_rate": round(profit_rate, 2),
            "quantity_before": qty_before,
            "sell_quantity": sell_qty,
            "sell_type": sell_type,
            "order_type": self._order_type,
            "order_result": order_result,
            "order_id": order_id,
            "error_message": error,
        }

    def _append_log(self, entry: dict) -> None:
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self._log_file.exists()
            with open(self._log_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.LOG_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow({k: entry.get(k, "") for k in self.LOG_COLUMNS})
        except Exception as e:
            logger.error("auto_sell: CSV 로그 저장 실패: %s", e)

    def read_log(self) -> list[dict]:
        """CSV 로그 전체 읽기."""
        if not self._log_file.exists():
            return []
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []

    @property
    def state_file_path(self) -> Path:
        return self._state_file

    @property
    def log_file_path(self) -> Path:
        return self._log_file
