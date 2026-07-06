"""
master_pullback_top3_service.py — 고수 눌림목 오전 Top3 동시 감시 자동매매 서비스

전략명: MASTER_PULLBACK_TOP3_MULTI_ENTRY
오전 9:15~10:00: Top3 종목 중 눌림 조건 만족 종목 모두 순차 매수
오전 이후 10:00~14:30: 강화 조건 만족 종목만 재진입
상태파일: data/state/master_pullback_top3_multi_entry_YYYYMMDD.json
"""
from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, timedelta, time as _dt_time
from pathlib import Path

from app.logger import logger
from app.services.intraday_budget_allocator import IntradayBudgetAllocator
from app.strategy.intraday_indicators import (
    calculate_vwap,
    calculate_ema,
    calculate_rsi,
    calculate_ema_slope,
    calculate_volume_ratio,
    detect_bullish_reversal_1m,
    detect_williams_fractal_buy,
    resample_1m_to_3m,
    calculate_intraday_high_pullback,
)

# ── 상태 상수 ─────────────────────────────────────────────────────────────────
STATUS_WAITING   = "WAITING_ENTRY"
STATUS_PENDING   = "BUY_ORDER_PENDING"
STATUS_HOLDING   = "HOLDING"
STATUS_HALF_SOLD = "HALF_SOLD"
STATUS_COOLING   = "COOLING_DOWN"
STATUS_DONE      = "DONE"
STATUS_ERROR     = "ERROR"

STRATEGY_NAME = "MASTER_PULLBACK_TOP3_MULTI_ENTRY"
_ROOT = Path(__file__).resolve().parent.parent.parent

_LOG_COLUMNS = [
    "timestamp", "action", "symbol", "name", "quantity", "price",
    "reason", "sell_type", "order_success", "order_id", "error",
    "morning_score", "second_score",
]

# ── 시간 경계 (tuple → datetime.time 변환용) ──────────────────────────────────
_MORNING_START        = (9, 15)   # 09:15
_MORNING_END          = (10, 0)   # 10:00 (exclusive)
_MORNING_PHASE2_START = (9, 45)   # 09:45 — score threshold 완화
_SECOND_END           = (14, 30)  # 14:30
_FORCE_SELL           = (15, 10)  # 15:10

# ── 점수 임계값 ───────────────────────────────────────────────────────────────
_MORNING_SCORE_THRESHOLD_EARLY = 75   # 09:15~09:45
_MORNING_SCORE_THRESHOLD_LATE  = 70   # 09:45~10:00
_SECOND_SCORE_THRESHOLD        = 85   # 10:00 이후 재진입


def _t(h: int, m: int) -> _dt_time:
    return _dt_time(h, m)


class MasterPullbackTop3Service:
    """고수 눌림목 오전 Top3 동시 감시 자동매매 서비스."""

    def __init__(self, broker, kis_client=None, cfg=None):
        from app.config import get_config
        self._cfg = cfg or get_config()
        self.broker = broker
        self.kis_client = kis_client

        ic = self._cfg._raw.get("master_pullback_top3", {})
        self.total_budget: float = float(ic.get("total_budget", 10_000_000))
        self.cooldown_minutes: int = int(ic.get("cooldown_minutes", 10))
        self.allow_second_entry: bool = bool(ic.get("allow_second_entry", True))
        self.max_entries_per_symbol: int = int(ic.get("max_entries_per_symbol", 2))
        self.max_total_entries_per_day: int = int(ic.get("max_total_entries_per_day", 6))
        self.use_morning_budget_ratio: float = float(ic.get("use_morning_budget_ratio", 1.0))
        self.order_sleep_sec: float = float(ic.get("order_sleep_sec", 0.4))

        today = datetime.now().strftime("%Y%m%d")
        tmpl = ic.get("state_file", "data/state/master_pullback_top3_multi_entry_YYYYMMDD.json")
        log_tmpl = ic.get("log_file", "data/logs/master_pullback_top3_YYYYMMDD.csv")
        self.state_file = _ROOT / tmpl.replace("YYYYMMDD", today)
        self.log_file   = _ROOT / log_tmpl.replace("YYYYMMDD", today)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # ── 런타임 상태 ───────────────────────────────────────────────────────
        self.target_top3: list[str] = []
        self.symbols_state: dict[str, dict] = {}
        self.total_entries_today: int = 0
        self.allocated_budget_by_symbol: dict[str, float] = {}
        self._prev_rsi: dict[str, float] = {}

        self.load_state()

    # ── Top3 로드 ─────────────────────────────────────────────────────────────

    def load_top3(self, top3: list[dict]) -> None:
        allocated = IntradayBudgetAllocator().allocate(top3, self.total_budget)
        self.target_top3 = [s["symbol"] for s in allocated]
        for stock in allocated:
            sym = stock["symbol"]
            self.allocated_budget_by_symbol[sym] = float(stock["allocated_budget"])
            if sym not in self.symbols_state:
                self.symbols_state[sym] = self._new_symbol_state(stock)
            else:
                self.symbols_state[sym]["allocated_budget"] = stock["allocated_budget"]
                self.symbols_state[sym]["name"] = stock.get("name", self.symbols_state[sym].get("name", ""))
                self.symbols_state[sym]["rank"] = stock.get("rank", self.symbols_state[sym].get("rank", 0))
        self.save_state()

    def _new_symbol_state(self, stock: dict) -> dict:
        return {
            "symbol": stock["symbol"],
            "name": stock.get("name", ""),
            "rank": stock.get("rank", 0),
            "final_score": float(stock.get("final_score", 0) or 0),
            "allocated_budget": float(stock.get("allocated_budget", 0)),
            "allocated_weight": float(stock.get("allocated_weight", 0)),
            "entries_count": 0,
            "position_quantity": 0,
            "avg_buy_price": 0.0,
            "current_price": float(stock.get("current_price", 0) or 0),
            "highest_price_after_entry": 0.0,
            "morning_entry_done": False,
            "last_exit_type": "",
            "half_sold_done": False,
            "full_sold_done": False,
            "cooldown_until": "",
            "last_buy_at": "",
            "last_sell_at": "",
            "status": STATUS_WAITING,
            "last_buy_flag": False,
            "last_sell_flag": "",
            "last_reason": "",
            "morning_score": 0,
            "second_score": 0,
            "realized_pnl": 0.0,
            "order_history": [],
        }

    # ── 메인 루프 ──────────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        now = datetime.now()
        summary = {
            "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": STRATEGY_NAME,
            "actions": [],
            "symbols": {},
        }

        if now.time() >= _t(*_FORCE_SELL):
            results = self._force_sell_all(now)
            summary["actions"].extend(results)
            self.save_state()
            for sym in self.symbols_state:
                summary["symbols"][sym] = self.symbols_state[sym]["status"]
            return summary

        for sym, state in self.symbols_state.items():
            if state["status"] == STATUS_COOLING:
                self._try_release_cooldown(state, now)

            current_price = self._get_current_price(sym, state)
            if current_price > 0:
                state["current_price"] = current_price

            if state["status"] in (STATUS_HOLDING, STATUS_HALF_SOLD):
                hp = state.get("highest_price_after_entry", 0.0)
                if current_price > hp:
                    state["highest_price_after_entry"] = current_price

            summary["symbols"][sym] = state["status"]

        candle_cache: dict[str, list[dict]] = {}

        # ── 오전 매수 구간: 순차 처리 ─────────────────────────────────────────
        in_morning = _t(*_MORNING_START) <= now.time() < _t(*_MORNING_END)
        if in_morning:
            for sym in self.target_top3:
                state = self.symbols_state.get(sym)
                if state is None:
                    continue
                if state["status"] != STATUS_WAITING:
                    continue
                if state.get("morning_entry_done", False):
                    continue

                candles = candle_cache.setdefault(sym, self._get_candles_1m(sym))
                current_price = state.get("current_price", 0.0)

                score, hard_blocked, block_reason = self._morning_buy_score(
                    sym, state, candles, current_price, now
                )
                state["morning_score"] = score

                if hard_blocked:
                    state["last_buy_flag"] = False
                    state["last_reason"] = block_reason
                    continue

                threshold = (
                    _MORNING_SCORE_THRESHOLD_LATE
                    if now.time() >= _t(*_MORNING_PHASE2_START)
                    else _MORNING_SCORE_THRESHOLD_EARLY
                )

                if score >= threshold:
                    state["last_buy_flag"] = True
                    result = self._execute_morning_buy(sym, state, current_price)
                    summary["actions"].append({"symbol": sym, "action": "morning_buy", **result})
                    if result.get("success"):
                        state["morning_entry_done"] = True
                        if self.order_sleep_sec > 0:
                            time.sleep(self.order_sleep_sec)
                else:
                    state["last_buy_flag"] = False
                    state["last_reason"] = f"score_insufficient({score}<{threshold})"

        # ── 오전 이후 재진입 구간 ─────────────────────────────────────────────
        in_second = (
            now.time() >= _t(*_MORNING_END)
            and now.time() < _t(*_SECOND_END)
            and self.allow_second_entry
        )
        if in_second:
            for sym in self.target_top3:
                state = self.symbols_state.get(sym)
                if state is None:
                    continue
                if state["status"] != STATUS_WAITING:
                    continue
                if state.get("last_exit_type") == "stop_loss":
                    continue
                if not state.get("morning_entry_done", False):
                    continue
                if state["entries_count"] >= self.max_entries_per_symbol:
                    continue
                if self.total_entries_today >= self.max_total_entries_per_day:
                    continue
                cu = state.get("cooldown_until", "")
                if cu:
                    try:
                        if datetime.now() < datetime.fromisoformat(cu):
                            continue
                    except Exception:
                        pass

                candles = candle_cache.setdefault(sym, self._get_candles_1m(sym))
                current_price = state.get("current_price", 0.0)
                score, hard_ok, block_reason = self._second_entry_score(
                    sym, state, candles, current_price
                )
                state["second_score"] = score

                if not hard_ok:
                    state["last_buy_flag"] = False
                    state["last_reason"] = block_reason
                    continue

                if score >= _SECOND_SCORE_THRESHOLD:
                    state["last_buy_flag"] = True
                    result = self._execute_second_buy(sym, state, current_price)
                    summary["actions"].append({"symbol": sym, "action": "second_buy", **result})
                    if result.get("success") and self.order_sleep_sec > 0:
                        time.sleep(self.order_sleep_sec)
                else:
                    state["last_buy_flag"] = False
                    state["last_reason"] = f"second_score_low({score}<{_SECOND_SCORE_THRESHOLD})"

        # ── 매도 감시 ─────────────────────────────────────────────────────────
        for sym, state in self.symbols_state.items():
            if state["status"] not in (STATUS_HOLDING, STATUS_HALF_SOLD):
                continue
            candles = candle_cache.setdefault(sym, self._get_candles_1m(sym))
            current_price = state.get("current_price", 0.0)
            sell_type, sell_reason = self._check_sell_flag(sym, state, candles, current_price, now)
            state["last_sell_flag"] = sell_type
            if sell_type:
                result = self._execute_sell(sym, state, sell_type, current_price, sell_reason)
                summary["actions"].append({
                    "symbol": sym, "action": "sell", "sell_type": sell_type, **result
                })

        self.save_state()
        for sym in self.symbols_state:
            summary["symbols"][sym] = self.symbols_state[sym]["status"]
        return summary

    # ── 오전 Buy Score ────────────────────────────────────────────────────────

    def _morning_buy_score(
        self,
        symbol: str,
        state: dict,
        candles_1m: list[dict],
        current_price: float,
        now: datetime,
    ) -> tuple[int, bool, str]:
        """Returns (score, hard_blocked, block_reason)."""
        if symbol not in self.target_top3:
            return 0, True, "not_in_top3"
        if state.get("morning_entry_done", False):
            return 0, True, "morning_entry_already_done"
        if state.get("entries_count", 0) >= 1:
            return 0, True, "already_entered_today"
        if current_price < 20_000:
            return 0, True, f"price_too_low({current_price})"
        if len(candles_1m) < 10:
            return 0, True, "insufficient_candles"

        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        raw_day_open = candles_1m[-1]["open"] if candles_1m else current_price
        # 최신봉 집합이 고점 이후 하락 구간이면 oldest open > current → min open을 day_open 근사치로 사용
        if raw_day_open > current_price:
            day_open = min((c["open"] for c in candles_1m), default=current_price)
        else:
            day_open = raw_day_open
        change_rate = (current_price - day_open) / day_open * 100.0 if day_open > 0 else 0.0
        if change_rate < 1.0:
            return 0, True, f"change_rate_too_low({change_rate:.1f}%)"
        if change_rate > 12.0:
            return 0, True, f"change_rate_too_high({change_rate:.1f}%)"

        vwap = calculate_vwap(candles_1m)
        vwap_diff_pct = 0.0
        if vwap > 0:
            vwap_diff_pct = (current_price - vwap) / vwap * 100.0
            if vwap_diff_pct < -0.8:
                return 0, True, f"below_vwap({vwap_diff_pct:.2f}%)"

        pullback_pct = calculate_intraday_high_pullback(current_price, intraday_high)
        if pullback_pct <= -5.0:
            return 0, True, f"price_crashed({pullback_pct:.1f}%)"

        rsi = calculate_rsi(candles_1m)
        if rsi >= 80:
            return 0, True, f"rsi_overbought({rsi:.1f})"

        budget = self.allocated_budget_by_symbol.get(symbol, state.get("allocated_budget", 0))
        morning_budget = budget * self.use_morning_budget_ratio
        if current_price > 0 and int(morning_budget / current_price) < 1:
            return 0, True, "insufficient_budget_for_1_share"

        # ── Score 계산 ─────────────────────────────────────────────────────────
        score = 0

        score += 25  # Top3 종목

        rank = state.get("rank", 0)
        if rank == 1:
            score += 10  # Top3 1위

        if vwap > 0:
            if current_price > vwap:
                score += 15
            elif vwap_diff_pct >= -0.35:
                score += 10

        candles_3m = resample_1m_to_3m(candles_1m)
        ema20, ema50, ema100 = [], [], []
        if len(candles_3m) >= 20:
            ema20 = calculate_ema(candles_3m, 20)
        if len(candles_3m) >= 50:
            ema50 = calculate_ema(candles_3m, 50)
        if len(candles_3m) >= 100:
            ema100 = calculate_ema(candles_3m, 100)

        if ema20 and ema50 and ema100:
            if ema20[0] > ema50[0] > ema100[0]:
                score += 15
            elif ema20[0] > ema50[0]:
                score += 10
        elif ema20 and ema50 and ema20[0] > ema50[0]:
            score += 10

        if len(ema20) >= 2 and calculate_ema_slope(ema20) > 0:
            score += 8

        if -3.5 <= pullback_pct <= -0.8:
            score += 15

        if detect_bullish_reversal_1m(candles_1m):
            score += 10

        prev_rsi = self._prev_rsi.get(symbol, rsi)
        self._prev_rsi[symbol] = rsi
        if 40 <= rsi <= 72:
            score += 8
        if prev_rsi < 55 <= rsi:
            score += 10

        vol_ratio = calculate_volume_ratio(candles_1m)
        if vol_ratio >= 1.2:
            score += 8
        elif vol_ratio >= 1.0:
            score += 5

        if detect_williams_fractal_buy(candles_1m):
            score += 10

        return score, False, ""

    # ── 2차 진입 Score ────────────────────────────────────────────────────────

    def _second_entry_score(
        self,
        symbol: str,
        state: dict,
        candles_1m: list[dict],
        current_price: float,
    ) -> tuple[int, bool, str]:
        """Returns (score, hard_conditions_met, block_reason)."""
        if len(candles_1m) < 10:
            return 0, False, "insufficient_candles"

        vwap = calculate_vwap(candles_1m)
        if vwap <= 0 or current_price <= vwap:
            return 0, False, "not_above_vwap"

        candles_3m = resample_1m_to_3m(candles_1m)
        ema20, ema50 = [], []
        if len(candles_3m) >= 20:
            ema20 = calculate_ema(candles_3m, 20)
        if len(candles_3m) >= 50:
            ema50 = calculate_ema(candles_3m, 50)

        if not (ema20 and ema50 and ema20[0] > ema50[0]):
            return 0, False, "ema20_below_ema50"
        if not (len(ema20) >= 2 and calculate_ema_slope(ema20) > 0):
            return 0, False, "ema20_slope_negative"

        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        pullback_pct = calculate_intraday_high_pullback(current_price, intraday_high)

        if pullback_pct <= -4.0:
            return 0, False, f"price_too_far_from_high({pullback_pct:.1f}%)"
        if not (-3.0 <= pullback_pct <= -1.2):
            return 0, False, f"pullback_out_of_range({pullback_pct:.1f}%)"

        if not detect_bullish_reversal_1m(candles_1m):
            return 0, False, "no_bullish_reversal"

        vol_ratio = calculate_volume_ratio(candles_1m)
        if vol_ratio < 1.2:
            return 0, False, f"volume_ratio_low({vol_ratio:.2f})"

        rsi = calculate_rsi(candles_1m)
        if not (45 <= rsi <= 68):
            return 0, False, f"rsi_out_of_range({rsi:.1f})"

        prev_rsi = self._prev_rsi.get(symbol, rsi)
        self._prev_rsi[symbol] = rsi
        rsi_55_cross = (prev_rsi < 55 <= rsi)
        fractal_buy = detect_williams_fractal_buy(candles_1m)
        if not (rsi_55_cross or fractal_buy):
            return 0, False, "no_rsi55_cross_or_fractal"

        score = 0
        score += 20  # VWAP 위
        score += 15  # EMA20 > EMA50
        score += 10  # EMA20 slope 양수
        score += 15  # 눌림 -1.2%~-3.0%
        score += 15  # 1분봉 양봉 전환
        if vol_ratio >= 1.2:
            score += 10
        if 45 <= rsi <= 68:
            score += 10
        if rsi_55_cross:
            score += 10
        if fractal_buy:
            score += 10

        return score, True, ""

    # ── Sell Flag ─────────────────────────────────────────────────────────────

    def _check_sell_flag(
        self,
        symbol: str,
        state: dict,
        candles_1m: list[dict],
        current_price: float,
        now: datetime,
    ) -> tuple[str, str]:
        avg_buy = state.get("avg_buy_price", 0.0)
        if avg_buy <= 0 or current_price <= 0:
            return "", ""

        profit_rate = (current_price - avg_buy) / avg_buy * 100.0

        # 1. 손절 -0.9% (최우선)
        if profit_rate <= -0.9:
            return "stop_loss", f"profit={profit_rate:.2f}%"

        # 2. 15:10 강제청산
        if now.time() >= _t(*_FORCE_SELL):
            return "force_close", "force_sell_15:10"

        # 3. +2.2% 전량익절 (절반익절 미완료여도 우선 전량매도)
        if profit_rate >= 2.2:
            return "full_tp", f"profit={profit_rate:.2f}%"

        # 4. +1.35% 절반익절 (1회만)
        if profit_rate >= 1.35 and not state.get("half_sold_done", False):
            return "half_tp", f"profit={profit_rate:.2f}%"

        # 5. 고점 대비 -1.2% trailing stop
        highest = state.get("highest_price_after_entry", 0.0)
        if highest > 0:
            trail = (current_price - highest) / highest * 100.0
            if trail <= -1.2:
                return "trailing_stop", f"trail={trail:.2f}%"

        if not candles_1m:
            return "", ""

        # 6. VWAP -0.4% 이탈
        vwap = calculate_vwap(candles_1m)
        if vwap > 0:
            vwap_diff = (current_price - vwap) / vwap * 100.0
            if vwap_diff <= -0.4:
                return "vwap_break", f"vwap_diff={vwap_diff:.2f}%"

        # 7. 3분봉 EMA20 EMA50 추세 이탈
        candles_3m = resample_1m_to_3m(candles_1m)
        if len(candles_3m) >= 50:
            ema20 = calculate_ema(candles_3m, 20)
            ema50 = calculate_ema(candles_3m, 50)
            if ema20 and ema50 and ema20[0] < ema50[0]:
                return "ema_cross", "ema20_below_ema50"

        return "", ""

    # ── 매수 실행 (오전) ──────────────────────────────────────────────────────

    def _execute_morning_buy(self, symbol: str, state: dict, current_price: float) -> dict:
        if current_price <= 0:
            return {"success": False, "reason": "no_price"}
        budget = self.allocated_budget_by_symbol.get(symbol, state.get("allocated_budget", 0))
        morning_budget = budget * self.use_morning_budget_ratio
        quantity = int(morning_budget / current_price)
        if quantity < 1:
            state["last_reason"] = "qty_too_small"
            return {"success": False, "reason": "qty_too_small"}
        return self._do_buy(symbol, state, current_price, quantity, entry_type="morning")

    # ── 매수 실행 (2차) ────────────────────────────────────────────────────────

    def _execute_second_buy(self, symbol: str, state: dict, current_price: float) -> dict:
        if current_price <= 0:
            return {"success": False, "reason": "no_price"}
        budget = self.allocated_budget_by_symbol.get(symbol, state.get("allocated_budget", 0))
        quantity = int(budget / current_price)
        if quantity < 1:
            state["last_reason"] = "qty_too_small"
            return {"success": False, "reason": "qty_too_small"}
        return self._do_buy(symbol, state, current_price, quantity, entry_type="second")

    def _do_buy(self, symbol: str, state: dict, current_price: float, quantity: int, entry_type: str) -> dict:
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
            logger.error(f"[MasterPB] 매수 예외 {symbol}: {e}")
            state["status"] = STATUS_ERROR
            state["last_reason"] = str(e)
            return {"success": False, "reason": str(e)}

        if success:
            state["status"] = STATUS_HOLDING
            state["avg_buy_price"] = current_price
            state["position_quantity"] = quantity
            state["entries_count"] = state.get("entries_count", 0) + 1
            state["last_buy_at"] = datetime.now().isoformat()
            state["highest_price_after_entry"] = current_price
            state["half_sold_done"] = False
            state["full_sold_done"] = False
            state["last_exit_type"] = ""
            state["order_history"].append({
                "action": "buy", "type": entry_type,
                "price": current_price, "qty": quantity,
                "at": state["last_buy_at"],
            })
            self.total_entries_today += 1
            logger.info(f"[MasterPB] {entry_type} 매수 성공 {symbol} {quantity}주 @{current_price:,}")
        else:
            state["status"] = STATUS_ERROR
            state["last_reason"] = "buy_failed"

        self.save_state()
        self._log_trade(
            "buy", symbol, state.get("name", ""), quantity, current_price,
            entry_type, "", success, order_id, "",
            state.get("morning_score", 0), state.get("second_score", 0),
        )
        return {"success": success, "order_id": order_id, "quantity": quantity, "price": current_price}

    # ── 매도 실행 ──────────────────────────────────────────────────────────────

    def _execute_sell(
        self, symbol: str, state: dict, sell_type: str, current_price: float, sell_reason: str
    ) -> dict:
        pos_qty = state.get("position_quantity", 0)
        if pos_qty <= 0:
            return {"success": False, "reason": "no_position"}

        sell_qty = math.ceil(pos_qty * 0.5) if sell_type == "half_tp" else pos_qty

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
            logger.error(f"[MasterPB] 매도 예외 {symbol}: {e}")
            state["last_reason"] = str(e)
            return {"success": False, "reason": str(e)}

        if success:
            avg_buy = state.get("avg_buy_price", current_price)
            pnl = (current_price - avg_buy) * sell_qty
            state["realized_pnl"] = state.get("realized_pnl", 0.0) + pnl
            state["last_sell_at"] = datetime.now().isoformat()
            state["order_history"].append({
                "action": "sell", "type": sell_type,
                "price": current_price, "qty": sell_qty,
                "at": state["last_sell_at"],
            })

            if sell_type == "half_tp":
                state["status"] = STATUS_HALF_SOLD
                state["half_sold_done"] = True
                state["position_quantity"] = pos_qty - sell_qty
            else:
                state["position_quantity"] = 0
                state["last_exit_type"] = sell_type
                cooldown_until = datetime.now() + timedelta(minutes=self.cooldown_minutes)
                state["cooldown_until"] = cooldown_until.isoformat()
                state["status"] = STATUS_COOLING

            logger.info(
                f"[MasterPB] 매도 성공 {symbol} {sell_qty}주 @{current_price:,} "
                f"[{sell_type}] PnL={pnl:+,.0f}"
            )

        self.save_state()
        self._log_trade(
            "sell", symbol, state.get("name", ""), sell_qty, current_price,
            sell_reason, sell_type, success, order_id, "",
            state.get("morning_score", 0), state.get("second_score", 0),
        )
        return {"success": success, "order_id": order_id, "sell_type": sell_type, "quantity": sell_qty, "price": current_price}

    # ── 강제 전량 청산 ─────────────────────────────────────────────────────────

    def _force_sell_all(self, now: datetime) -> list[dict]:
        results = []
        for sym, state in self.symbols_state.items():
            if state["status"] in (STATUS_HOLDING, STATUS_HALF_SOLD):
                price = state.get("current_price", state.get("avg_buy_price", 0.0))
                if price <= 0:
                    continue
                result = self._execute_sell(sym, state, "force_close", price, "force_sell_15:10")
                results.append({"symbol": sym, "action": "force_sell", **result})
        return results

    # ── 쿨다운 ────────────────────────────────────────────────────────────────

    def _try_release_cooldown(self, state: dict, now: datetime) -> None:
        cu = state.get("cooldown_until", "")
        if cu:
            try:
                if now >= datetime.fromisoformat(cu):
                    state["status"] = STATUS_WAITING
                    state["cooldown_until"] = ""
            except Exception:
                pass

    # ── 가격/캔들 조회 ────────────────────────────────────────────────────────

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

    def _get_candles_1m(self, symbol: str) -> list[dict]:
        if self.kis_client is None:
            return []
        try:
            return self.kis_client.get_minute_candles(symbol, period_min=1, count=60)
        except Exception as e:
            logger.warning(f"[MasterPB] 분봉 조회 실패 {symbol}: {e}")
            return []

    # ── 상태 저장/복원 ────────────────────────────────────────────────────────

    def save_state(self) -> None:
        data = {
            "date": datetime.now().strftime("%Y%m%d"),
            "strategy_name": STRATEGY_NAME,
            "target_top3": self.target_top3,
            "total_budget": self.total_budget,
            "allocated_budget_by_symbol": self.allocated_budget_by_symbol,
            "total_entries_today": self.total_entries_today,
            "symbols_state": self.symbols_state,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[MasterPB] 상태 저장 실패: {e}")

    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.now().strftime("%Y%m%d")
            if data.get("date") != today:
                return
            self.target_top3 = data.get("target_top3", [])
            self.total_budget = float(data.get("total_budget", self.total_budget))
            self.allocated_budget_by_symbol = data.get("allocated_budget_by_symbol", {})
            self.total_entries_today = data.get("total_entries_today", 0)
            self.symbols_state = data.get("symbols_state", {})
            logger.info(f"[MasterPB] 상태 복원: {len(self.symbols_state)}종목")
        except Exception as e:
            logger.warning(f"[MasterPB] 상태 로드 실패: {e}")

    # ── 거래 로그 ─────────────────────────────────────────────────────────────

    def _log_trade(
        self, action, symbol, name, qty, price, reason, sell_type,
        success, order_id, error, morning_score=0, second_score=0,
    ) -> None:
        try:
            write_header = not self.log_file.exists()
            with open(self.log_file, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": action, "symbol": symbol, "name": name,
                    "quantity": qty, "price": price, "reason": reason,
                    "sell_type": sell_type, "order_success": success,
                    "order_id": order_id, "error": error,
                    "morning_score": morning_score, "second_score": second_score,
                })
        except Exception as e:
            logger.warning(f"[MasterPB] 로그 기록 실패: {e}")
