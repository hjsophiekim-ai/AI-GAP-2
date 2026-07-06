"""
intraday_auto_trade_service.py — 완전 자동 장중매매 서비스

전략: 주도섹터 Top3 종목에 대해 1분봉/3분봉 기반으로 장중 자동매수/매도 수행.
상태머신: WAITING_ENTRY → BUY_ORDER_PENDING → HOLDING → HALF_SOLD → COOLING_DOWN → DONE / ERROR
"""
import csv
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.services.intraday_budget_allocator import IntradayBudgetAllocator
from app.strategy.intraday_indicators import (
    calculate_vwap,
    calculate_ema,
    calculate_rsi,
    resample_1m_to_3m,
    detect_bullish_reversal_1m,
    detect_bearish_volume_candle_1m,
    calculate_intraday_high_pullback,
)

STATUS_WAITING = "WAITING_ENTRY"
STATUS_PENDING = "BUY_ORDER_PENDING"
STATUS_HOLDING = "HOLDING"
STATUS_HALF_SOLD = "HALF_SOLD"
STATUS_COOLING = "COOLING_DOWN"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"

_ROOT = Path(__file__).resolve().parent.parent.parent

_LOG_COLUMNS = [
    "timestamp", "action", "symbol", "name", "quantity", "price",
    "reason", "sell_type", "order_success", "order_id", "error",
]


class IntradayAutoTradeService:
    """장중 자동매매 서비스 — 주도섹터 Top3 상태머신."""

    def __init__(self, broker, kis_client=None, cfg=None):
        from app.config import get_config
        self._cfg = cfg or get_config()
        self.broker = broker
        self.kis_client = kis_client

        ic = self._cfg._raw.get("intraday_auto_trade", {})
        self.total_budget: float = float(ic.get("total_budget", 10_000_000))
        self.max_position_count: int = int(ic.get("max_position_count", 3))
        self.check_interval_seconds: int = int(ic.get("check_interval_seconds", 10))
        self.buy_start_time: str = ic.get("buy_start_time", "09:10")
        self.buy_end_time: str = ic.get("buy_end_time", "14:40")
        self.force_sell_time: str = ic.get("force_sell_time", "15:10")
        self.max_total_entries_per_day: int = int(ic.get("max_total_entries_per_day", 3))
        self.max_entries_per_symbol: int = int(ic.get("max_entries_per_symbol", 2))
        self.cooldown_minutes: int = int(ic.get("cooldown_minutes", 10))
        self.allow_breakout: bool = bool(ic.get("allow_breakout_entry_if_no_pullback", True))

        buy_cond = ic.get("buy_conditions", {})
        self.min_pullback_pct: float = float(buy_cond.get("min_pullback_pct", -3.8))
        self.max_pullback_pct: float = float(buy_cond.get("max_pullback_pct", -1.2))
        self.min_volume_ratio: float = float(buy_cond.get("min_volume_ratio", 1.15))
        self.min_rsi: float = float(buy_cond.get("min_rsi", 42.0))
        self.max_rsi: float = float(buy_cond.get("max_rsi", 72.0))
        self.crash_threshold_pct: float = float(buy_cond.get("crash_threshold_pct", -5.0))

        relaxed = ic.get("relaxed_buy_conditions", {})
        self.relaxed_min_pullback: float = float(relaxed.get("min_pullback_pct", -0.8))
        self.relaxed_min_vol_ratio: float = float(relaxed.get("min_volume_ratio", 1.0))

        sell_cond = ic.get("sell_conditions", {})
        self.stop_loss_pct: float = float(sell_cond.get("stop_loss_pct", -1.2))
        self.half_tp_pct: float = float(sell_cond.get("half_take_profit_pct", 1.8))
        self.full_tp_pct: float = float(sell_cond.get("full_take_profit_pct", 3.2))
        self.trailing_stop_pct: float = float(sell_cond.get("trailing_stop_pct", -1.8))

        today = datetime.now().strftime("%Y%m%d")
        state_tmpl = ic.get("state_file", "data/state/intraday_auto_trade_state_YYYYMMDD.json")
        log_tmpl = ic.get("log_file", "data/logs/intraday_auto_trades_YYYYMMDD.csv")
        self.state_file = _ROOT / state_tmpl.replace("YYYYMMDD", today)
        self.log_file = _ROOT / log_tmpl.replace("YYYYMMDD", today)

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # 런타임 상태
        self.symbols_state: dict[str, dict] = {}
        self.total_entries_today: int = 0
        self.breakout_entries_today: int = 0
        self.prev_rsi: dict[str, float] = {}

        self.load_state()

    # ── Top3 종목 로드 ─────────────────────────────────────────────────────

    def load_top3(self, top3: list[dict]) -> None:
        allocated = IntradayBudgetAllocator().allocate(top3, self.total_budget)
        for stock in allocated:
            sym = stock.get("symbol", "")
            if not sym:
                continue
            if sym in self.symbols_state:
                # 기존 상태 유지 (재시작 복원)
                self.symbols_state[sym]["allocated_budget"] = stock["allocated_budget"]
                self.symbols_state[sym]["allocated_weight"] = stock["allocated_weight"]
            else:
                self.symbols_state[sym] = {
                    "symbol": sym,
                    "name": stock.get("name", ""),
                    "rank": stock.get("rank", 0),
                    "allocated_budget": stock["allocated_budget"],
                    "allocated_weight": stock["allocated_weight"],
                    "entries_count": 0,
                    "position_quantity": 0,
                    "avg_buy_price": 0.0,
                    "current_price": float(stock.get("current_price", 0) or 0),
                    "highest_price_after_entry": 0.0,
                    "first_take_profit_done": False,
                    "second_take_profit_done": False,
                    "last_buy_at": "",
                    "last_sell_at": "",
                    "cooldown_until": "",
                    "status": STATUS_WAITING,
                    "last_buy_flag": False,
                    "last_sell_flag": "",
                    "last_reason": "",
                    "realized_pnl": 0.0,
                    "order_history": [],
                }
        self.save_state()

    # ── 메인 루프 ──────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        now = datetime.now()
        summary = {
            "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "actions": [],
            "symbols": {},
        }

        # 강제청산 시간 체크
        force_time = self._parse_time(self.force_sell_time)
        if now.time() >= force_time:
            results = self._force_sell_all()
            summary["actions"].extend(results)

        for sym, state in self.symbols_state.items():
            # 쿨다운 종료 체크
            if state["status"] == STATUS_COOLING:
                cooldown_until = state.get("cooldown_until", "")
                if cooldown_until:
                    try:
                        cu = datetime.fromisoformat(cooldown_until)
                        if now >= cu:
                            state["status"] = STATUS_WAITING
                            state["cooldown_until"] = ""
                    except Exception:
                        pass

            current_price = self._get_current_price(sym, state)
            if current_price > 0:
                state["current_price"] = current_price

            # 장중 고점 갱신
            if state["status"] in (STATUS_HOLDING, STATUS_HALF_SOLD):
                if current_price > state.get("highest_price_after_entry", 0):
                    state["highest_price_after_entry"] = current_price

            candles_1m = self._get_candles_1m(sym)

            if state["status"] == STATUS_WAITING:
                flag, reason = self._check_buy_flag(sym, state, candles_1m)
                state["last_buy_flag"] = flag
                state["last_reason"] = reason
                if flag:
                    result = self._execute_buy(sym, state, current_price)
                    summary["actions"].append({"symbol": sym, "action": "buy", **result})

            elif state["status"] in (STATUS_HOLDING, STATUS_HALF_SOLD):
                sell_type, reason = self._check_sell_flag(sym, state, candles_1m, current_price)
                state["last_sell_flag"] = sell_type
                if sell_type:
                    result = self._execute_sell(sym, state, sell_type, current_price)
                    summary["actions"].append({"symbol": sym, "action": "sell", "sell_type": sell_type, **result})

            summary["symbols"][sym] = state["status"]

        self.save_state()
        return summary

    # ── 분봉 조회 ──────────────────────────────────────────────────────────

    def _get_candles_1m(self, symbol: str) -> list[dict]:
        if self.kis_client is None:
            return []
        try:
            candles = self.kis_client.get_minute_candles(symbol, period_min=1, count=60)
            if not candles:
                logger.warning(f"[Intraday] 1분봉 빈 응답 {symbol} (장 마감 또는 API 오류)")
            return candles or []
        except Exception as e:
            logger.warning(f"[Intraday] 1분봉 조회 실패 {symbol}: {e}")
            return []

    def _get_current_price(self, symbol: str, state: dict) -> float:
        if self.kis_client is None:
            return state.get("current_price", 0.0)
        try:
            data = self.kis_client.get_current_price(symbol)
            if data:
                return float(data.get("current_price", 0) or 0)
        except Exception:
            pass
        return state.get("current_price", 0.0)

    # ── Buy Flag ───────────────────────────────────────────────────────────

    def _check_buy_flag(self, symbol: str, state: dict, candles_1m: list[dict]) -> tuple[bool, str]:
        now = datetime.now()

        # 시간 체크
        buy_start = self._parse_time(self.buy_start_time)
        buy_end = self._parse_time(self.buy_end_time)
        if not (buy_start <= now.time() < buy_end):
            return False, "outside_buy_window"

        # 진입 횟수 체크
        if self.total_entries_today >= self.max_total_entries_per_day:
            return False, "max_total_entries_reached"
        if state["entries_count"] >= self.max_entries_per_symbol:
            return False, "max_symbol_entries_reached"

        current_price = state.get("current_price", 0.0)
        if current_price <= 0:
            return False, "no_price_data"

        if len(candles_1m) < 5:
            return False, "insufficient_candle_data"

        # 3분봉 최소 5개 미만이면 지표 계산 불가
        candles_3m_pre = resample_1m_to_3m(candles_1m)
        if len(candles_3m_pre) < 5:
            logger.warning(
                f"[Intraday] 3분봉 부족 {symbol}: 1분봉={len(candles_1m)}개 "
                f"→ 3분봉={len(candles_3m_pre)}개 (최소 5개 필요)"
            )
            return False, "insufficient_3m_candles"

        # 표준 조건 체크
        ok, reason = self._standard_buy_check(symbol, state, candles_1m, current_price)
        if ok:
            return True, reason

        # 완화 조건 (10:00 이후, 당일 진입 0회)
        if now.hour >= 10 and self.total_entries_today == 0:
            ok2, reason2 = self._relaxed_buy_check(symbol, state, candles_1m, current_price)
            if ok2:
                return True, "relaxed_" + reason2

        # 돌파 진입
        if self.allow_breakout and self.breakout_entries_today < 1:
            ok3, reason3 = self._breakout_buy_check(symbol, state, candles_1m, current_price)
            if ok3:
                self.breakout_entries_today += 1
                return True, "breakout_" + reason3

        return False, reason or "no_buy_signal"

    def _standard_buy_check(self, symbol: str, state: dict, candles_1m: list[dict], current_price: float) -> tuple[bool, str]:
        vwap = calculate_vwap(candles_1m)
        if vwap > 0 and current_price <= vwap:
            return False, "below_vwap"

        candles_3m = resample_1m_to_3m(candles_1m)
        if len(candles_3m) >= 20:
            ema5 = calculate_ema(candles_3m, 5)
            ema20 = calculate_ema(candles_3m, 20)
            if ema5 and ema20 and ema5[0] <= ema20[0]:
                return False, "ema_reverse"

        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        pullback = calculate_intraday_high_pullback(current_price, intraday_high)
        if pullback < self.crash_threshold_pct:
            return False, "price_crashed"
        if not (self.min_pullback_pct <= pullback <= self.max_pullback_pct):
            return False, f"pullback_out_of_range({pullback:.2f}%)"

        if not detect_bullish_reversal_1m(candles_1m):
            return False, "no_bullish_reversal"

        latest_vol = candles_1m[0]["volume"]
        prior_vols = [c["volume"] for c in candles_1m[1:4]]
        avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior > 0 and latest_vol < avg_prior * self.min_volume_ratio:
            return False, "volume_insufficient"

        rsi = calculate_rsi(candles_1m)
        self.prev_rsi[symbol] = rsi
        if not (self.min_rsi <= rsi <= self.max_rsi):
            return False, f"rsi_out_of_range({rsi:.1f})"

        return True, "standard_buy"

    def _relaxed_buy_check(self, symbol: str, state: dict, candles_1m: list[dict], current_price: float) -> tuple[bool, str]:
        vwap = calculate_vwap(candles_1m)
        if vwap > 0 and current_price <= vwap:
            return False, "below_vwap"

        candles_3m = resample_1m_to_3m(candles_1m)
        if len(candles_3m) >= 20:
            ema5 = calculate_ema(candles_3m, 5)
            ema20 = calculate_ema(candles_3m, 20)
            if ema5 and ema20 and ema5[0] <= ema20[0]:
                return False, "ema_reverse"

        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        pullback = calculate_intraday_high_pullback(current_price, intraday_high)
        if pullback < self.relaxed_min_pullback:
            return False, "pullback_too_deep"

        latest_vol = candles_1m[0]["volume"]
        prior_vols = [c["volume"] for c in candles_1m[1:4]]
        avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior > 0 and latest_vol < avg_prior * self.relaxed_min_vol_ratio:
            return False, "volume_insufficient"

        rsi = calculate_rsi(candles_1m)
        if not (self.min_rsi <= rsi <= self.max_rsi):
            return False, f"rsi_out_of_range({rsi:.1f})"

        return True, "relaxed_buy"

    def _breakout_buy_check(self, symbol: str, state: dict, candles_1m: list[dict], current_price: float) -> tuple[bool, str]:
        vwap = calculate_vwap(candles_1m)
        if vwap > 0 and current_price <= vwap:
            return False, "below_vwap"

        candles_3m = resample_1m_to_3m(candles_1m)
        if len(candles_3m) >= 20:
            ema5 = calculate_ema(candles_3m, 5)
            ema20 = calculate_ema(candles_3m, 20)
            if ema5 and ema20 and ema5[0] <= ema20[0]:
                return False, "ema_reverse"

        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        pullback = calculate_intraday_high_pullback(current_price, intraday_high)
        if pullback < -0.5:
            return False, "not_near_high"

        rsi = calculate_rsi(candles_1m)
        if rsi > self.max_rsi:
            return False, "rsi_overbought"

        latest_vol = candles_1m[0]["volume"]
        prior_vols = [c["volume"] for c in candles_1m[1:4]]
        avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior > 0 and latest_vol < avg_prior * 1.0:
            return False, "volume_not_increasing"

        return True, "breakout"

    # ── Sell Flag ──────────────────────────────────────────────────────────

    def _check_sell_flag(self, symbol: str, state: dict, candles_1m: list[dict], current_price: float) -> tuple[str, str]:
        avg_buy = state.get("avg_buy_price", 0.0)
        if avg_buy <= 0 or current_price <= 0:
            return "", ""

        profit_rate = (current_price - avg_buy) / avg_buy * 100.0

        # 1. 손절
        if profit_rate <= self.stop_loss_pct:
            return "stop_loss", f"profit_rate={profit_rate:.2f}%"

        # 2. 강제청산
        now = datetime.now()
        force_time = self._parse_time(self.force_sell_time)
        if now.time() >= force_time:
            return "force_close", "force_sell_time_reached"

        # 3. 전량익절 (+3.2%)
        if profit_rate >= self.full_tp_pct:
            return "full_tp", f"profit_rate={profit_rate:.2f}%"

        # 4. 절반익절 (+1.8%, 1회만)
        if profit_rate >= self.half_tp_pct and not state.get("first_take_profit_done", False):
            return "half_tp", f"profit_rate={profit_rate:.2f}%"

        # 5. 트레일링 스탑 (고점 대비 -1.8%)
        highest = state.get("highest_price_after_entry", 0.0)
        if highest > 0:
            trail_rate = (current_price - highest) / highest * 100.0
            if trail_rate <= self.trailing_stop_pct:
                return "trailing_stop", f"trail_rate={trail_rate:.2f}%"

        if not candles_1m:
            return "", ""

        vwap = calculate_vwap(candles_1m)
        if vwap > 0 and current_price < vwap:
            return "vwap_break", f"price={current_price} vwap={vwap:.0f}"

        candles_3m = resample_1m_to_3m(candles_1m)
        if len(candles_3m) >= 20:
            ema5 = calculate_ema(candles_3m, 5)
            ema20 = calculate_ema(candles_3m, 20)
            if ema5 and ema20 and ema5[0] < ema20[0]:
                return "ema_cross", "ema5_below_ema20"

        cur_rsi = calculate_rsi(candles_1m)
        prev_rsi = self.prev_rsi.get(symbol, cur_rsi)
        self.prev_rsi[symbol] = cur_rsi
        if prev_rsi >= 75 and cur_rsi < prev_rsi:
            return "rsi_peak", f"rsi {prev_rsi:.1f}→{cur_rsi:.1f}"

        if detect_bearish_volume_candle_1m(candles_1m):
            return "bearish_candle", "bearish_volume_spike"

        return "", ""

    # ── 매수 실행 ──────────────────────────────────────────────────────────

    def _execute_buy(self, symbol: str, state: dict, current_price: float) -> dict:
        if current_price <= 0:
            return {"success": False, "reason": "no_price"}

        quantity = int(state["allocated_budget"] / current_price)
        if quantity < 1:
            return {"success": False, "reason": "qty_too_small"}

        # 실전모드 안전장치
        mode = getattr(self.broker, "mode", "dry_run")
        if mode == "real":
            safety = self._cfg._raw.get("safety", {})
            if not safety.get("enable_real_buy", False):
                return {"success": False, "reason": "real_buy_disabled"}

        try:
            result = self.broker.buy(symbol, quantity, int(current_price))
            success = result.get("success", False) if isinstance(result, dict) else False
            order_id = result.get("order_id", "") if isinstance(result, dict) else ""
        except Exception as e:
            logger.error(f"[Intraday] 매수 예외 {symbol}: {e}")
            state["status"] = STATUS_ERROR
            state["last_reason"] = str(e)
            return {"success": False, "reason": str(e)}

        if success:
            state["status"] = STATUS_HOLDING
            state["avg_buy_price"] = current_price
            state["position_quantity"] = quantity
            state["entries_count"] += 1
            state["last_buy_at"] = datetime.now().isoformat()
            state["highest_price_after_entry"] = current_price
            state["first_take_profit_done"] = False
            state["order_history"].append({"action": "buy", "price": current_price, "qty": quantity, "at": state["last_buy_at"]})
            self.total_entries_today += 1
            logger.info(f"[Intraday] 매수 성공 {symbol} {quantity}주 @{current_price:,}")
        else:
            state["status"] = STATUS_ERROR
            state["last_reason"] = "buy_failed"

        self.save_state()
        self._log_trade("buy", symbol, state.get("name", ""), quantity, current_price, "buy_flag", "", success, order_id, "")
        return {"success": success, "order_id": order_id, "quantity": quantity, "price": current_price}

    # ── 매도 실행 ──────────────────────────────────────────────────────────

    def _execute_sell(self, symbol: str, state: dict, sell_type: str, current_price: float) -> dict:
        pos_qty = state.get("position_quantity", 0)
        if pos_qty <= 0:
            return {"success": False, "reason": "no_position"}

        if sell_type == "half_tp":
            sell_qty = math.ceil(pos_qty * 0.5)
        else:
            sell_qty = pos_qty

        mode = getattr(self.broker, "mode", "dry_run")
        if mode == "real":
            safety = self._cfg._raw.get("safety", {})
            if not safety.get("enable_real_sell", False):
                return {"success": False, "reason": "real_sell_disabled"}

        try:
            result = self.broker.sell(symbol, sell_qty, int(current_price))
            success = result.get("success", False) if isinstance(result, dict) else False
            order_id = result.get("order_id", "") if isinstance(result, dict) else ""
        except Exception as e:
            logger.error(f"[Intraday] 매도 예외 {symbol}: {e}")
            state["last_reason"] = str(e)
            return {"success": False, "reason": str(e)}

        if success:
            avg_buy = state.get("avg_buy_price", current_price)
            pnl = (current_price - avg_buy) * sell_qty
            state["realized_pnl"] = state.get("realized_pnl", 0.0) + pnl
            state["last_sell_at"] = datetime.now().isoformat()
            state["order_history"].append({"action": "sell", "type": sell_type, "price": current_price, "qty": sell_qty, "at": state["last_sell_at"]})

            if sell_type == "half_tp":
                state["status"] = STATUS_HALF_SOLD
                state["first_take_profit_done"] = True
                state["position_quantity"] -= sell_qty
            else:
                state["position_quantity"] = 0
                cooldown_until = datetime.now() + timedelta(minutes=self.cooldown_minutes)
                state["cooldown_until"] = cooldown_until.isoformat()
                state["status"] = STATUS_COOLING

            logger.info(f"[Intraday] 매도 성공 {symbol} {sell_qty}주 @{current_price:,} [{sell_type}] PnL={pnl:+,.0f}")

        self.save_state()
        self._log_trade("sell", symbol, state.get("name", ""), sell_qty, current_price, sell_type, sell_type, success, order_id, "")
        return {"success": success, "order_id": order_id, "sell_type": sell_type, "quantity": sell_qty, "price": current_price}

    # ── 강제 전량 청산 ─────────────────────────────────────────────────────

    def _force_sell_all(self) -> list[dict]:
        results = []
        for sym, state in self.symbols_state.items():
            if state["status"] in (STATUS_HOLDING, STATUS_HALF_SOLD):
                price = state.get("current_price", 0.0)
                if price <= 0:
                    price = state.get("avg_buy_price", 1.0)
                result = self._execute_sell(sym, state, "force_close", price)
                results.append({"symbol": sym, "action": "force_sell", **result})
        return results

    # ── 상태 저장/복원 ─────────────────────────────────────────────────────

    def save_state(self) -> None:
        data = {
            "date": datetime.now().strftime("%Y%m%d"),
            "total_entries_today": self.total_entries_today,
            "breakout_entries_today": self.breakout_entries_today,
            "total_budget": self.total_budget,
            "symbols": self.symbols_state,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Intraday] 상태 저장 실패: {e}")

    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.now().strftime("%Y%m%d")
            if data.get("date") != today:
                return  # 다른 날 상태 파일 무시
            self.total_entries_today = data.get("total_entries_today", 0)
            self.breakout_entries_today = data.get("breakout_entries_today", 0)
            self.symbols_state = data.get("symbols", {})
            logger.info(f"[Intraday] 상태 복원: {len(self.symbols_state)}종목")
        except Exception as e:
            logger.warning(f"[Intraday] 상태 로드 실패: {e}")

    # ── 거래 로그 ──────────────────────────────────────────────────────────

    def _log_trade(self, action: str, symbol: str, name: str, qty: int, price: float, reason: str, sell_type: str, success: bool, order_id: str, error: str) -> None:
        try:
            write_header = not self.log_file.exists()
            with open(self.log_file, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": action,
                    "symbol": symbol,
                    "name": name,
                    "quantity": qty,
                    "price": price,
                    "reason": reason,
                    "sell_type": sell_type,
                    "order_success": success,
                    "order_id": order_id,
                    "error": error,
                })
        except Exception as e:
            logger.warning(f"[Intraday] 로그 기록 실패: {e}")

    # ── 유틸 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(time_str: str):
        """'HH:MM' 형식을 datetime.time으로 변환."""
        from datetime import time as dtime
        parts = time_str.split(":")
        return dtime(int(parts[0]), int(parts[1]))


# ─────────────────────────────────────────────────────────────────────────────
# Top3TimedBuyService — top3_timed_buy_3pct_takeprofit 전략
# 복잡한 1분봉/VWAP/EMA/RSI 조건 없이 시간 기반 순차매수 + +3% 자동익절
# ─────────────────────────────────────────────────────────────────────────────

_TIMED_LOG_COLUMNS = [
    "timestamp", "action", "symbol", "name", "rank",
    "quantity", "price", "order_amount", "profit_rate",
    "reason", "status", "order_no", "error_message",
]

_TIMED_STRATEGY_NAME = "top3_timed_buy_3pct_takeprofit"


class Top3TimedBuyService:
    """시간 분산 매수 + 3% 자동익절 전략 서비스."""

    def __init__(self, broker, kis_client=None, cfg=None):
        from app.config import get_config
        self._cfg = cfg or get_config()
        self.broker = broker
        self.kis_client = kis_client

        ic = self._cfg._raw.get("intraday_auto_trade", {})
        sched = ic.get("buy_schedule", {})
        alloc = ic.get("budget_allocation", {})

        self.buy_window_start: str = ic.get("buy_window_start", "09:10")
        self.buy_window_end: str = ic.get("buy_window_end", "09:30")
        self.buy_schedule: dict = {
            1: sched.get("rank1", "09:12"),
            2: sched.get("rank2", "09:16"),
            3: sched.get("rank3", "09:20"),
        }
        self.budget_alloc: dict = {
            1: float(alloc.get("rank1", 0.45)),
            2: float(alloc.get("rank2", 0.35)),
            3: float(alloc.get("rank3", 0.20)),
        }
        self.check_interval_seconds: int = int(ic.get("check_interval_seconds", 10))
        self.take_profit_pct: float = float(ic.get("take_profit_pct", 3.0))
        self.stop_loss_pct: float = float(ic.get("stop_loss_pct", -1.2))
        self.stop_loss_enabled: bool = bool(ic.get("stop_loss_enabled", True))
        self.force_exit_time: str = ic.get("force_exit_time", "15:10")

        self.min_change_rate_at_buy: float = float(ic.get("min_change_rate_at_buy", -1.0))
        self.max_drop_from_intraday_high_pct: float = float(ic.get("max_drop_from_intraday_high_pct", 5.0))
        self.safety_filter_enabled: bool = bool(ic.get("minimum_safety_filter_enabled", True))

        today = datetime.now().strftime("%Y%m%d")
        self.state_file = _ROOT / f"data/state/{_TIMED_STRATEGY_NAME}_{today}.json"
        self.log_file = _ROOT / f"data/logs/{_TIMED_STRATEGY_NAME}_{today}.csv"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        self.total_budget: float = 0.0
        self.symbols_state: dict[str, dict] = {}

        # 매수/매도 독립 제어 플래그
        self.enable_auto_buy: bool = True   # False이면 매수 단계 건너뜀
        self.enable_auto_sell: bool = True  # False이면 매도 단계 건너뜀

        self.load_state()

    # ── Top3 종목 로드 ─────────────────────────────────────────────────────

    def load_from_positions(
        self,
        positions: list,
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = -1.5,
    ) -> None:
        """
        자동매도 전용 모드: 현재 보유종목(포지션)을 감시 상태로 로드.
        enable_auto_buy=False일 때 사용. 이미 보유 중인 종목을 HOLDING 상태로 등록해
        익절/손절 조건 도달 시 자동매도한다.

        Parameters
        ----------
        positions : broker.get_positions() 또는 KISClient.get_balance()['positions'] 결과
        take_profit_pct  : 익절 기준 (%) 예: 3.0
        stop_loss_pct    : 손절 기준 (%) 예: -1.5
        """
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct

        for pos in positions:
            # Position 객체 또는 dict 모두 지원
            sym = str(getattr(pos, "symbol", None) or pos.get("symbol", "")).strip()
            name = str(getattr(pos, "name", None) or pos.get("name", sym))
            qty = int(getattr(pos, "quantity", None) or pos.get("quantity", 0))
            avg_price = float(getattr(pos, "avg_price", None) or pos.get("avg_price", 0) or 0)
            cur_price = float(getattr(pos, "current_price", None) or pos.get("current_price", avg_price) or avg_price)
            if not sym or qty <= 0 or avg_price <= 0:
                continue
            if sym in self.symbols_state:
                continue  # 당일 상태 유지
            self.symbols_state[sym] = {
                "date": datetime.now().strftime("%Y%m%d"),
                "strategy_name": _TIMED_STRATEGY_NAME,
                "symbol": sym,
                "name": name,
                "rank": 0,
                "allocated_budget": round(avg_price * qty),
                "buy_scheduled_time": "00:00",
                "bought_today": True,       # 이미 매수 완료
                "buy_time": "",
                "buy_price": avg_price,
                "buy_quantity": qty,
                "avg_buy_price": avg_price,
                "current_price": cur_price,
                "profit_rate": round((cur_price - avg_price) / avg_price * 100.0, 4) if avg_price > 0 else 0.0,
                "take_profit_pct": take_profit_pct,
                "stop_loss_pct": stop_loss_pct,
                "sold_today": False,
                "sell_time": "",
                "sell_price": 0.0,
                "sell_reason": "",
                "order_no": "",
                "status": "HOLDING",
                "last_checked_at": "",
                "last_error": "",
            }
        logger.info(
            f"[TimedBuy] 자동매도 전용: {len(self.symbols_state)}종목 HOLDING 등록 "
            f"(익절 +{take_profit_pct:.1f}% / 손절 {stop_loss_pct:.1f}%)"
        )
        self.save_state()

    def load_top3(self, top3: list[dict], total_budget: float) -> None:
        self.total_budget = total_budget
        for stock in top3:
            rank = int(stock.get("rank", 0))
            sym = str(stock.get("symbol", "")).strip()
            if not sym or rank not in (1, 2, 3):
                continue
            if sym in self.symbols_state:
                continue  # 당일 상태 유지
            weight = self.budget_alloc.get(rank, 0.2)
            self.symbols_state[sym] = {
                "date": datetime.now().strftime("%Y%m%d"),
                "strategy_name": _TIMED_STRATEGY_NAME,
                "symbol": sym,
                "name": stock.get("name", ""),
                "rank": rank,
                "allocated_budget": round(total_budget * weight),
                "buy_scheduled_time": self.buy_schedule.get(rank, "09:20"),
                "bought_today": False,
                "buy_time": "",
                "buy_price": 0.0,
                "buy_quantity": 0,
                "avg_buy_price": 0.0,
                "current_price": float(stock.get("current_price", 0) or 0),
                "profit_rate": 0.0,
                "take_profit_pct": self.take_profit_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "sold_today": False,
                "sell_time": "",
                "sell_price": 0.0,
                "sell_reason": "",
                "order_no": "",
                "status": "WAITING",
                "last_checked_at": "",
                "last_error": "",
            }
        self._handle_extra_budget()
        self.save_state()

    def _handle_extra_budget(self) -> None:
        """배분 후 잔여 예산을 rank1에 추가 (1주 이상 매수 가능 시)."""
        total_alloc = sum(s["allocated_budget"] for s in self.symbols_state.values())
        leftover = self.total_budget - total_alloc
        rank1 = next((s for s in self.symbols_state.values() if s["rank"] == 1), None)
        if rank1 and leftover > 0:
            rank1["allocated_budget"] += leftover

    # ── 메인 루프 ──────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        now = datetime.now()
        summary = {"checked_at": now.strftime("%Y-%m-%d %H:%M:%S"), "actions": [], "symbols": {}}

        force_time = self._parse_time(self.force_exit_time)
        is_force_exit = now.time() >= force_time

        for sym, state in self.symbols_state.items():
            state["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

            current_price = self._get_current_price(sym, state)
            if current_price > 0:
                state["current_price"] = current_price
                if state["bought_today"] and state["avg_buy_price"] > 0:
                    state["profit_rate"] = round(
                        (current_price - state["avg_buy_price"]) / state["avg_buy_price"] * 100.0, 4
                    )

            # 강제청산: enable_auto_sell=True인 경우에만 실행
            if is_force_exit and self.enable_auto_sell and state["bought_today"] and not state["sold_today"]:
                result = self._execute_sell(sym, state, current_price, "force_exit")
                summary["actions"].append({"symbol": sym, "action": "force_sell", **result})

            # 자동매수: enable_auto_buy=True이고 아직 미매수인 경우
            elif not state["bought_today"] and self.enable_auto_buy:
                sched_time = self._parse_time(state["buy_scheduled_time"])
                buy_end = self._parse_time(self.buy_window_end)
                if now.time() >= sched_time and now.time() < buy_end:
                    ok, skip_reason = self._safety_check(sym, state, current_price)
                    if ok:
                        result = self._execute_buy(sym, state, current_price)
                        summary["actions"].append({"symbol": sym, "action": "buy", **result})
                    else:
                        state["last_error"] = f"safety_skip: {skip_reason}"

            # 자동매도: enable_auto_sell=True이고 보유 중인 경우
            elif state["bought_today"] and not state["sold_today"] and self.enable_auto_sell:
                profit_rate = state.get("profit_rate", 0.0)
                sell_reason = ""
                if profit_rate >= self.take_profit_pct:
                    sell_reason = "take_profit"
                elif self.stop_loss_enabled and profit_rate <= self.stop_loss_pct:
                    sell_reason = "stop_loss"
                if sell_reason:
                    result = self._execute_sell(sym, state, current_price, sell_reason)
                    summary["actions"].append({"symbol": sym, "action": "sell", **result})

            summary["symbols"][sym] = state.get("status", "UNKNOWN")

        self.save_state()
        return summary

    # ── 안전조건 검사 ──────────────────────────────────────────────────────

    def _safety_check(self, symbol: str, state: dict, current_price: float) -> tuple[bool, str]:
        if not self.safety_filter_enabled:
            return True, ""
        if current_price <= 0:
            return False, "no_price"
        budget = state.get("allocated_budget", 0)
        if budget <= 0 or current_price <= 0 or int(budget / current_price) < 1:
            return False, "qty_zero"

        if self.kis_client:
            try:
                data = self.kis_client.get_current_price(symbol)
                if data:
                    prev_close = float(data.get("prev_close_price", 0) or 0)
                    intraday_high = float(data.get("high_price", current_price) or current_price)
                    if prev_close > 0:
                        change_rate = (current_price - prev_close) / prev_close * 100.0
                        if change_rate < self.min_change_rate_at_buy:
                            return False, f"change_rate_too_low({change_rate:.2f}%)"
                    if intraday_high > 0:
                        drop_from_high = (current_price - intraday_high) / intraday_high * 100.0
                        if drop_from_high < -abs(self.max_drop_from_intraday_high_pct):
                            return False, f"drop_from_high_too_large({drop_from_high:.2f}%)"
            except Exception:
                pass  # 조회 실패 시 안전조건 통과 (skip하지 않음)
        return True, ""

    # ── 현재가 조회 ────────────────────────────────────────────────────────

    def _get_current_price(self, symbol: str, state: dict) -> float:
        if self.kis_client is None:
            return state.get("current_price", 0.0)
        try:
            data = self.kis_client.get_current_price(symbol)
            if data:
                return float(data.get("current_price", 0) or 0)
        except Exception:
            pass
        return state.get("current_price", 0.0)

    # ── 매수 실행 ──────────────────────────────────────────────────────────

    def _execute_buy(self, symbol: str, state: dict, current_price: float) -> dict:
        if current_price <= 0:
            state["last_error"] = "no_price"
            return {"success": False, "reason": "no_price"}

        broker_mode = getattr(self.broker, "mode", "mock")
        if broker_mode == "real":
            safety = self._cfg._raw.get("safety", {})
            if not safety.get("enable_real_trading", False) or not safety.get("enable_real_buy", False):
                state["last_error"] = "real_buy_disabled"
                return {"success": False, "reason": "real_buy_disabled"}

        # 실계좌: 종목별 매수가능금액(nrcvb_buy_amt 포함) vs 배분예산 중 작은 값 사용
        # 우선순위: 1) get_stock_buyable_amount(symbol)  2) get_orderable_cash()  3) allocated
        # 절대 get_balance()(=인출가능금액 withdrawable)를 매수 기준으로 쓰지 않는다.
        allocated = state.get("allocated_budget", 0)
        if broker_mode == "real":
            try:
                if hasattr(self.broker, "get_stock_buyable_amount"):
                    buyable = self.broker.get_stock_buyable_amount(symbol, int(current_price))
                elif hasattr(self.broker, "get_orderable_cash"):
                    buyable = self.broker.get_orderable_cash()
                else:
                    buyable = allocated
                safe_budget = min(allocated, math.floor(buyable * 0.98))
            except Exception:
                safe_budget = allocated
        else:
            safe_budget = allocated

        quantity = int(safe_budget / current_price)
        if quantity < 1:
            state["last_error"] = "qty_too_small"
            return {"success": False, "reason": "qty_too_small"}

        def _do_buy(qty):
            result = self.broker.buy(symbol, qty, int(current_price))
            ok = result.get("success", False) if isinstance(result, dict) else bool(result)
            ono = result.get("order_id", result.get("order_no", "")) if isinstance(result, dict) else ""
            msg = result.get("message", "") if isinstance(result, dict) else ""
            return ok, ono, msg

        try:
            success, order_no, msg = _do_buy(quantity)
            # 잔고 부족 오류 시: 최신 매수가능금액 재조회 후 0.95배 수량 1회 재시도
            if not success and broker_mode == "real" and "잔고부족" in msg:
                try:
                    if hasattr(self.broker, "get_stock_buyable_amount"):
                        refreshed = self.broker.get_stock_buyable_amount(symbol, int(current_price))
                        # 재조회 금액의 95% vs 배분예산 중 작은 값으로 재계산
                        retry_qty = int(math.floor(min(allocated, refreshed) * 0.95 / current_price))
                    else:
                        retry_qty = int(quantity * 0.95)
                except Exception:
                    retry_qty = int(quantity * 0.95)
                if retry_qty >= 1:
                    logger.warning(f"[TimedBuy] 잔고부족 재시도 {symbol} {quantity}→{retry_qty}주")
                    success, order_no, msg = _do_buy(retry_qty)
                    if success:
                        quantity = retry_qty
                    else:
                        logger.error(
                            f"[TimedBuy] 재시도도 실패 {symbol}: msg_cd/msg1 확인 필요 | msg={msg}"
                        )
        except Exception as e:
            logger.error(f"[TimedBuy] 매수 예외 {symbol}: {e}")
            state["last_error"] = str(e)
            state["status"] = "ERROR"
            return {"success": False, "reason": str(e)}

        if success:
            state["bought_today"] = True
            state["buy_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["buy_price"] = current_price
            state["buy_quantity"] = quantity
            state["avg_buy_price"] = current_price
            state["order_no"] = order_no
            state["status"] = "HOLDING"
            state["last_error"] = ""
            logger.info(f"[TimedBuy] 매수 성공 {symbol}({state['name']}) rank{state['rank']} {quantity}주 @{current_price:,}")
        else:
            state["last_error"] = "buy_failed"
            state["status"] = "BUY_FAILED"

        self._log_trade("buy", state, quantity, current_price, "", order_no, "")
        return {"success": success, "order_no": order_no, "quantity": quantity, "price": current_price}

    # ── 매도 실행 ──────────────────────────────────────────────────────────

    def _execute_sell(self, symbol: str, state: dict, current_price: float, reason: str) -> dict:
        quantity = state.get("buy_quantity", 0)
        if quantity < 1:
            return {"success": False, "reason": "no_position"}

        if current_price <= 0:
            current_price = state.get("avg_buy_price", 1.0)

        broker_mode = getattr(self.broker, "mode", "mock")
        if broker_mode == "real":
            safety = self._cfg._raw.get("safety", {})
            if not safety.get("enable_real_trading", False) or not safety.get("enable_real_sell", False):
                state["last_error"] = "real_sell_disabled"
                return {"success": False, "reason": "real_sell_disabled"}

        try:
            result = self.broker.sell(symbol, quantity, int(current_price))
            success = result.get("success", False) if isinstance(result, dict) else bool(result)
            order_no = result.get("order_id", result.get("order_no", "")) if isinstance(result, dict) else ""
        except Exception as e:
            logger.error(f"[TimedBuy] 매도 예외 {symbol}: {e}")
            state["last_error"] = str(e)
            return {"success": False, "reason": str(e)}

        if success:
            profit_rate = state.get("profit_rate", 0.0)
            state["sold_today"] = True
            state["sell_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["sell_price"] = current_price
            state["sell_reason"] = reason
            state["status"] = "SOLD"
            state["last_error"] = ""
            logger.info(f"[TimedBuy] 매도 성공 {symbol} {quantity}주 @{current_price:,} [{reason}] {profit_rate:+.2f}%")
        else:
            state["last_error"] = "sell_failed"

        self._log_trade("sell", state, quantity, current_price, reason, order_no, "")
        return {"success": success, "order_no": order_no, "quantity": quantity, "price": current_price, "reason": reason}

    # ── 상태 저장/복원 ─────────────────────────────────────────────────────

    def save_state(self) -> None:
        data = {
            "date": datetime.now().strftime("%Y%m%d"),
            "strategy_name": _TIMED_STRATEGY_NAME,
            "total_budget": self.total_budget,
            "symbols": self.symbols_state,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[TimedBuy] 상태 저장 실패: {e}")

    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.now().strftime("%Y%m%d")
            if data.get("date") != today:
                return
            self.total_budget = float(data.get("total_budget", 0))
            self.symbols_state = data.get("symbols", {})
            logger.info(f"[TimedBuy] 상태 복원: {len(self.symbols_state)}종목")
        except Exception as e:
            logger.warning(f"[TimedBuy] 상태 로드 실패: {e}")

    # ── 상태 조회 헬퍼 ─────────────────────────────────────────────────────

    def get_status_list(self) -> list[dict]:
        return list(self.symbols_state.values())

    # ── 거래 로그 ──────────────────────────────────────────────────────────

    def _log_trade(self, action: str, state: dict, qty: int, price: float, reason: str, order_no: str, error: str) -> None:
        avg_buy = state.get("avg_buy_price", 0.0)
        profit_rate = state.get("profit_rate", 0.0)
        try:
            write_header = not self.log_file.exists()
            with open(self.log_file, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_TIMED_LOG_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": action,
                    "symbol": state.get("symbol", ""),
                    "name": state.get("name", ""),
                    "rank": state.get("rank", ""),
                    "quantity": qty,
                    "price": price,
                    "order_amount": qty * price,
                    "profit_rate": profit_rate,
                    "reason": reason,
                    "status": state.get("status", ""),
                    "order_no": order_no,
                    "error_message": error,
                })
        except Exception as e:
            logger.warning(f"[TimedBuy] 로그 기록 실패: {e}")

    # ── 유틸 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(time_str: str):
        from datetime import time as dtime
        parts = time_str.split(":")
        return dtime(int(parts[0]), int(parts[1]))
