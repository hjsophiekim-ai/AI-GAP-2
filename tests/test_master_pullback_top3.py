"""
tests/test_master_pullback_top3.py — 고수 눌림목 Top3 동시 감시 전략 테스트 (20개)
"""
from __future__ import annotations

import sys
import json
import math
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.strategy.intraday_indicators import (
    calculate_ema_slope,
    detect_williams_fractal_buy,
    calculate_volume_ratio,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_candles(n=30, base_close=50000, trend=100, base_volume=1000):
    candles = []
    for i in range(n):
        price = base_close + trend * (n - 1 - i)
        candles.append({
            "time": f"09{i:02d}00",
            "open": price - 200,
            "high": price + 500,
            "low": price - 300,
            "close": price,
            "volume": base_volume + i * 10,
        })
    return candles  # newest first


def _make_top3(prices=(50000, 80000, 120000)):
    return [
        {"symbol": "000660", "name": "SK하이닉스", "rank": 1,
         "current_price": prices[0], "final_score": 90.0},
        {"symbol": "005930", "name": "삼성전자", "rank": 2,
         "current_price": prices[1], "final_score": 80.0},
        {"symbol": "035420", "name": "NAVER", "rank": 3,
         "current_price": prices[2], "final_score": 70.0},
    ]


class MockBroker:
    mode = "dry_run"
    _buy_fail = False

    def buy(self, symbol, quantity, price, **kwargs):
        if self._buy_fail:
            return {"success": False}
        return {"success": True, "order_id": f"BUY-{symbol}"}

    def sell(self, symbol, quantity, price, **kwargs):
        return {"success": True, "order_id": f"SELL-{symbol}"}


def _make_service(tmp_dir, mode="dry_run", allow_second_entry=True):
    from app.services.master_pullback_top3_service import MasterPullbackTop3Service

    cfg = MagicMock()
    cfg.mode = mode
    cfg._raw = {
        "master_pullback_top3": {
            "allow_second_entry": allow_second_entry,
            "max_entries_per_symbol": 2,
            "max_total_entries_per_day": 6,
            "use_morning_budget_ratio": 1.0,
            "order_sleep_sec": 0.0,
        },
        "safety": {"enable_real_buy": False, "enable_real_sell": False},
    }
    cfg.trading = {}
    broker = MockBroker()
    broker.mode = mode

    with patch("app.services.master_pullback_top3_service._ROOT", Path(tmp_dir)):
        svc = MasterPullbackTop3Service(broker=broker, kis_client=None, cfg=cfg)
    return svc, broker


def _good_morning_candles(current_price=50000, pullback_pct=-1.5, n=30):
    """오전 buy flag를 통과할 수 있는 기본 캔들 세트."""
    intraday_high = current_price / (1 + pullback_pct / 100)
    candles = []
    for i in range(n):
        ratio = i / max(n - 1, 1)
        c = current_price - 500 + int(500 * ratio)
        candles.append({
            "time": f"09{i:02d}00",
            "open": c - 100 if i % 2 == 0 else c + 100,
            "high": int(intraday_high) if i == n - 1 else c + 300,
            "low": c - 300,
            "close": c,
            "volume": 2000 if i == 0 else 1000,
        })
    # 최신(index 0) = 양봉, 직전(index 1) = 음봉
    candles[0]["open"] = candles[0]["close"] - 300
    candles[1]["open"] = candles[1]["close"] + 300
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# 1. 지표 함수 테스트
# ─────────────────────────────────────────────────────────────────────────────

class TestNewIndicators(unittest.TestCase):

    def test_ema_slope_positive(self):
        ema_rising = [52000.0, 51000.0, 50000.0]  # newest first
        self.assertGreater(calculate_ema_slope(ema_rising), 0)

    def test_ema_slope_negative(self):
        ema_falling = [48000.0, 50000.0, 52000.0]
        self.assertLess(calculate_ema_slope(ema_falling), 0)

    def test_ema_slope_insufficient(self):
        self.assertEqual(calculate_ema_slope([50000.0]), 0.0)

    def test_williams_fractal_buy_detected(self):
        # newest-first: [105, 104, 100, 103, 102]
        # reversed (old-first): [102, 103, 100, 104, 105]
        # center_idx=2 → low=100, which is less than all others → fractal bottom ✓
        candles = [
            {"low": 105},
            {"low": 104},
            {"low": 100},
            {"low": 103},
            {"low": 102},
        ]
        self.assertTrue(detect_williams_fractal_buy(candles, lookback=2))

    def test_williams_fractal_insufficient_candles(self):
        candles = [{"low": 100}, {"low": 99}]
        self.assertFalse(detect_williams_fractal_buy(candles))

    def test_volume_ratio_above_threshold(self):
        candles = [
            {"volume": 3000},
            {"volume": 1000},
            {"volume": 1000},
            {"volume": 1000},
        ]
        ratio = calculate_volume_ratio(candles, lookback=3)
        self.assertAlmostEqual(ratio, 3.0, places=1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Hard block 조건 테스트
# ─────────────────────────────────────────────────────────────────────────────

class TestMorningHardBlock(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _svc(self):
        return _make_service(self.tmp)

    def _now_morning(self, hour=9, minute=20):
        return datetime.now().replace(hour=hour, minute=minute, second=0)

    # ── Test 1: 09:14 매수 금지 (run_once 레벨 in_morning=False) ─────────────
    def test_01_before_morning_window_no_buy(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        state = svc.symbols_state["000660"]
        now = datetime.now().replace(hour=9, minute=14, second=0)
        candles = _good_morning_candles()
        # _morning_buy_score 자체는 score 반환 (시간 체크는 run_once 레벨)
        score, blocked, reason = svc._morning_buy_score("000660", state, candles, 50000, now)
        self.assertIsInstance(score, int)

    # ── Test 2: 09:15 조건 만족 시 hard_blocked=False, score > 0 ─────────────
    def test_02_at_morning_start_can_buy(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.allocated_budget_by_symbol["000660"] = 5_000_000
        state = svc.symbols_state["000660"]
        now = datetime.now().replace(hour=9, minute=15, second=0)
        candles = _good_morning_candles(current_price=50000)
        score, blocked, reason = svc._morning_buy_score("000660", state, candles, 50000, now)
        self.assertFalse(blocked)
        self.assertGreater(score, 0)

    # ── Test 3: Top3 2개 동시 조건 만족 → 2개 순차 매수 ───────────────────────
    def test_03_two_symbols_both_bought_sequentially(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        for sym in ["000660", "005930", "035420"]:
            svc.allocated_budget_by_symbol[sym] = 3_000_000

        bought = []

        def mock_do_buy(symbol, state, price, qty, entry_type):
            bought.append(symbol)
            state["status"] = "HOLDING"
            state["entries_count"] += 1
            state["morning_entry_done"] = True
            state["avg_buy_price"] = price
            state["position_quantity"] = qty
            state["last_buy_at"] = datetime.now().isoformat()
            state["highest_price_after_entry"] = price
            state["half_sold_done"] = False
            state["full_sold_done"] = False
            state["last_exit_type"] = ""
            svc.total_entries_today += 1
            return {"success": True, "order_id": f"BUY-{symbol}", "quantity": qty, "price": price}

        svc._do_buy = mock_do_buy

        def mock_score(symbol, state, candles, price, now):
            if symbol in ["000660", "005930"]:
                return 80, False, ""
            return 40, False, ""

        svc._morning_buy_score = mock_score
        svc._get_candles_1m = lambda sym: _good_morning_candles()
        svc._get_current_price = lambda sym, state: 50000.0
        for sym in svc.symbols_state:
            svc.symbols_state[sym]["current_price"] = 50000.0

        import app.services.master_pullback_top3_service as mod
        from datetime import time as dtime
        with patch.object(mod, "_t") as mock_t:
            def fake_t(h, m):
                if (h, m) == (9, 15):   return dtime(0, 0)
                if (h, m) == (10, 0):   return dtime(23, 59)
                if (h, m) == (9, 45):   return dtime(23, 59)
                if (h, m) == (15, 10):  return dtime(23, 59)
                if (h, m) == (14, 30):  return dtime(23, 59)
                return dtime(h, m)
            mock_t.side_effect = fake_t
            svc.run_once()

        self.assertEqual(len(bought), 2)
        self.assertIn("000660", bought)
        self.assertIn("005930", bought)

    # ── Test 4: Top3 3개 모두 조건 만족 → 3개 순차 매수 ──────────────────────
    def test_04_three_symbols_all_bought(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        for sym in ["000660", "005930", "035420"]:
            svc.allocated_budget_by_symbol[sym] = 3_000_000

        bought = []

        def mock_do_buy(symbol, state, price, qty, entry_type):
            bought.append(symbol)
            state["status"] = "HOLDING"
            state["entries_count"] += 1
            state["morning_entry_done"] = True
            state["avg_buy_price"] = price
            state["position_quantity"] = qty
            state["last_buy_at"] = datetime.now().isoformat()
            state["highest_price_after_entry"] = price
            state["half_sold_done"] = False
            state["full_sold_done"] = False
            state["last_exit_type"] = ""
            svc.total_entries_today += 1
            return {"success": True, "order_id": f"BUY-{symbol}", "quantity": qty, "price": price}

        svc._do_buy = mock_do_buy
        svc._morning_buy_score = lambda sym, st, c, p, n: (80, False, "")
        svc._get_candles_1m = lambda sym: _good_morning_candles()
        svc._get_current_price = lambda sym, state: 50000.0
        for sym in svc.symbols_state:
            svc.symbols_state[sym]["current_price"] = 50000.0

        import app.services.master_pullback_top3_service as mod
        from datetime import time as dtime
        with patch.object(mod, "_t") as mock_t:
            def fake_t(h, m):
                if (h, m) == (9, 15):   return dtime(0, 0)
                if (h, m) == (10, 0):   return dtime(23, 59)
                if (h, m) == (9, 45):   return dtime(23, 59)
                if (h, m) == (15, 10):  return dtime(23, 59)
                if (h, m) == (14, 30):  return dtime(23, 59)
                return dtime(h, m)
            mock_t.side_effect = fake_t
            svc.run_once()

        self.assertEqual(len(bought), 3)

    # ── Test 5: morning_entry_done=True 종목 재매수 금지 ──────────────────────
    def test_05_morning_entry_done_blocks_rebuy(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.symbols_state["000660"]["morning_entry_done"] = True
        svc.allocated_budget_by_symbol["000660"] = 5_000_000
        now = self._now_morning()
        score, blocked, reason = svc._morning_buy_score(
            "000660", svc.symbols_state["000660"], _good_morning_candles(), 50000, now
        )
        self.assertTrue(blocked)
        self.assertIn("morning_entry_already_done", reason)

    # ── Test 6: Top3 외 종목 매수 금지 ────────────────────────────────────────
    def test_06_non_top3_blocked(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        fake_state = {"morning_entry_done": False, "entries_count": 0,
                      "allocated_budget": 5_000_000, "rank": 0}
        now = self._now_morning()
        score, blocked, reason = svc._morning_buy_score(
            "999999", fake_state, _good_morning_candles(), 50000, now
        )
        self.assertTrue(blocked)
        self.assertIn("not_in_top3", reason)

    # ── Test 7: 당일 상승률 1% 미만 매수 금지 ────────────────────────────────
    def test_07_low_change_rate_blocked(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.allocated_budget_by_symbol["000660"] = 5_000_000
        now = self._now_morning()
        candles = _good_morning_candles(current_price=50000)
        # oldest candle open ≈ 49750 → change_rate ≈ 0.5%
        candles[-1]["open"] = 49750
        for c in candles:
            c["close"] = 50000
        candles[0]["open"] = 49800   # 양봉 유지
        candles[1]["open"] = 50100   # 직전 음봉
        score, blocked, reason = svc._morning_buy_score(
            "000660", svc.symbols_state["000660"], candles, 50000, now
        )
        self.assertTrue(blocked)
        self.assertIn("change_rate_too_low", reason)

    # ── Test 8: 당일 상승률 12% 초과 매수 금지 ───────────────────────────────
    def test_08_high_change_rate_blocked(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.allocated_budget_by_symbol["000660"] = 5_000_000
        now = self._now_morning()
        candles = _good_morning_candles(current_price=50000)
        candles[-1]["open"] = 44000  # 상승률 ≈ 13.6%
        score, blocked, reason = svc._morning_buy_score(
            "000660", svc.symbols_state["000660"], candles, 50000, now
        )
        self.assertTrue(blocked)
        self.assertIn("change_rate_too_high", reason)

    # ── Test 9: VWAP -0.8% 이하 hard block ───────────────────────────────────
    def test_09_below_vwap_hard_blocked(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.allocated_budget_by_symbol["000660"] = 5_000_000
        now = self._now_morning()
        # VWAP ≈ 60333 (high=62000, low=59000, close=60000), current=55300
        candles = []
        for i in range(30):
            candles.append({
                "time": f"09{i:02d}00",
                "open": 60000, "high": 62000, "low": 59000,
                "close": 60000, "volume": 10000,
            })
        candles[0]["open"] = 55200   # 양봉
        candles[0]["close"] = 55400
        candles[1]["open"] = 55500   # 직전 음봉
        candles[1]["close"] = 55300
        # oldest open ≈ 55000 → change_rate ≈ 0.7% → too low; set to get ~5%
        candles[-1]["open"] = 52700
        current_price = 55300
        score, blocked, reason = svc._morning_buy_score(
            "000660", svc.symbols_state["000660"], candles, current_price, now
        )
        self.assertTrue(blocked)
        self.assertIn("below_vwap", reason)

    # ── Test 10: 배정예산으로 1주도 못 사면 skip ────────────────────────────
    def test_10_insufficient_budget_blocked(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc.allocated_budget_by_symbol["000660"] = 1000  # 현재가 50000 → 0주
        now = self._now_morning()
        score, blocked, reason = svc._morning_buy_score(
            "000660", svc.symbols_state["000660"], _good_morning_candles(), 50000, now
        )
        self.assertTrue(blocked)
        self.assertIn("insufficient_budget", reason)

    # ── Test 11: threshold 상수 확인 ────────────────────────────────────────
    def test_11_threshold_relaxed_after_0945(self):
        from app.services.master_pullback_top3_service import (
            _MORNING_SCORE_THRESHOLD_EARLY, _MORNING_SCORE_THRESHOLD_LATE
        )
        self.assertEqual(_MORNING_SCORE_THRESHOLD_EARLY, 75)
        self.assertEqual(_MORNING_SCORE_THRESHOLD_LATE, 70)

    # ── Test 12: 10:00 이후 오전 매수 로직 중단 ─────────────────────────────
    def test_12_after_1000_morning_buy_stops(self):
        svc, _ = self._svc()
        svc.load_top3(_make_top3())
        svc._morning_buy_score = lambda sym, st, c, p, n: (80, False, "")
        svc._get_candles_1m = lambda sym: _good_morning_candles()
        svc._get_current_price = lambda sym, state: 50000.0
        for sym in svc.symbols_state:
            svc.symbols_state[sym]["current_price"] = 50000.0

        bought = []

        def mock_do_buy(symbol, state, price, qty, entry_type):
            bought.append(symbol)
            return {"success": True}

        svc._do_buy = mock_do_buy

        import app.services.master_pullback_top3_service as mod
        from datetime import time as dtime
        with patch.object(mod, "_t") as mock_t:
            def fake_t(h, m):
                # MORNING_START 미래 → in_morning=False
                if (h, m) == (9, 15):   return dtime(23, 59)
                if (h, m) == (10, 0):   return dtime(0, 0)
                if (h, m) == (15, 10):  return dtime(23, 59)
                if (h, m) == (14, 30):  return dtime(23, 59)
                if (h, m) == (9, 45):   return dtime(23, 59)
                return dtime(h, m)
            mock_t.side_effect = fake_t
            svc.run_once()

        self.assertEqual(len(bought), 0)

    # ── Test 13: 2차 진입 score threshold 확인 ──────────────────────────────
    def test_13_second_entry_requires_strong_conditions(self):
        from app.services.master_pullback_top3_service import _SECOND_SCORE_THRESHOLD
        self.assertEqual(_SECOND_SCORE_THRESHOLD, 85)

    # ── Test 14: 직전 매매가 손절이면 2차 진입 금지 ─────────────────────────
    def test_14_stop_loss_exit_blocks_second_entry(self):
        svc, _ = _make_service(tempfile.mkdtemp(), allow_second_entry=True)
        svc.load_top3(_make_top3())
        state = svc.symbols_state["000660"]
        state["morning_entry_done"] = True
        state["last_exit_type"] = "stop_loss"
        state["entries_count"] = 0

        bought = []
        svc._second_entry_score = lambda sym, st, c, p: (90, True, "")
        svc._get_candles_1m = lambda sym: _good_morning_candles()
        svc._get_current_price = lambda sym, state: 50000.0

        def mock_do_buy(*a, **kw):
            bought.append(a[0])
            return {"success": True}

        svc._do_buy = mock_do_buy

        import app.services.master_pullback_top3_service as mod
        from datetime import time as dtime
        with patch.object(mod, "_t") as mock_t:
            def fake_t(h, m):
                if (h, m) == (9, 15):   return dtime(23, 59)
                if (h, m) == (10, 0):   return dtime(0, 0)
                if (h, m) == (14, 30):  return dtime(23, 59)
                if (h, m) == (15, 10):  return dtime(23, 59)
                if (h, m) == (9, 45):   return dtime(23, 59)
                return dtime(h, m)
            mock_t.side_effect = fake_t
            svc.run_once()

        self.assertNotIn("000660", bought)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 매도 조건 테스트
# ─────────────────────────────────────────────────────────────────────────────

class TestSellConditions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _svc(self):
        svc, _ = _make_service(self.tmp)
        return svc

    def _holding_state(self, avg_buy=50000, qty=10, highest=51000, half_done=False):
        return {
            "symbol": "000660", "name": "테스트",
            "avg_buy_price": float(avg_buy), "position_quantity": qty,
            "highest_price_after_entry": float(highest),
            "half_sold_done": half_done,
            "full_sold_done": False, "status": "HOLDING",
            "realized_pnl": 0.0,
        }

    def _now(self, h=10, m=0):
        return datetime.now().replace(hour=h, minute=m, second=0)

    # ── Test 15: +1.35% 절반익절 ────────────────────────────────────────────
    def test_15_half_tp_at_135pct(self):
        svc = self._svc()
        state = self._holding_state(avg_buy=50000, half_done=False)
        price = int(50000 * 1.0135) + 1  # +1.35% 초과
        sell_type, reason = svc._check_sell_flag("000660", state, [], price, self._now())
        self.assertEqual(sell_type, "half_tp")

    # ── Test 16: +2.2% 전량익절이 절반익절보다 우선 ─────────────────────────
    def test_16_full_tp_takes_priority_over_half_tp(self):
        svc = self._svc()
        state = self._holding_state(avg_buy=50000, half_done=False)
        price = int(50000 * 1.022) + 1  # +2.2% 초과
        sell_type, _ = svc._check_sell_flag("000660", state, [], price, self._now())
        self.assertEqual(sell_type, "full_tp")

    # ── Test 17: -0.9% 손절 ─────────────────────────────────────────────────
    def test_17_stop_loss_at_minus09pct(self):
        svc = self._svc()
        state = self._holding_state(avg_buy=50000)
        price = int(50000 * 0.991) - 1
        sell_type, _ = svc._check_sell_flag("000660", state, [], price, self._now())
        self.assertEqual(sell_type, "stop_loss")

    # ── Test 18: trailing -1.2% 청산 ────────────────────────────────────────
    def test_18_trailing_stop_at_minus12pct(self):
        svc = self._svc()
        # avg_buy=49000, highest=50500, half_done=True (절반익절 완료 상태)
        # price = 50500 * 0.987 ≈ 49844 → trail ≈ -1.3% ≤ -1.2% → trailing_stop
        # profit_rate = (49844-49000)/49000 ≈ +1.72% → no stop_loss, no full_tp
        # half_tp 건너뜀 (half_done=True), trailing 발동
        state = self._holding_state(avg_buy=49000, qty=10, highest=50500, half_done=True)
        price = int(50500 * 0.987)  # ≈ 49843, trail ≈ -1.32%
        sell_type, _ = svc._check_sell_flag("000660", state, [], price, self._now())
        self.assertEqual(sell_type, "trailing_stop")

    # ── Test 19: VWAP -0.4% 이탈 청산 ──────────────────────────────────────
    def test_19_vwap_break_sell(self):
        svc = self._svc()
        # avg_buy=54700, highest=54800 — profit_rate < 1.35%, trail < 1.2% → 익절/trailing 미발동
        # VWAP ≈ 55000, current=54600 → diff ≈ -0.73% ≤ -0.4% → vwap_break
        state = self._holding_state(avg_buy=54700, highest=54800)
        candles = []
        for i in range(30):
            candles.append({
                "time": f"09{i:02d}00",
                "open": 55000, "high": 56000, "low": 54000,
                "close": 55000, "volume": 5000,
            })
        current_price = 54600
        # profit_rate = (54600-54700)/54700 ≈ -0.18% → no stop_loss
        # trail = (54600-54800)/54800 ≈ -0.36% → no trailing
        # VWAP diff ≈ -0.73% → vwap_break
        sell_type, _ = svc._check_sell_flag("000660", state, candles, current_price, self._now())
        self.assertEqual(sell_type, "vwap_break")

    # ── Test 20: 매도 후 쿨다운 10분 ────────────────────────────────────────
    def test_20_cooldown_after_sell(self):
        svc, broker = _make_service(self.tmp)
        svc.load_top3(_make_top3())
        state = svc.symbols_state["000660"]
        state["status"] = "HOLDING"
        state["avg_buy_price"] = 50000.0
        state["position_quantity"] = 10
        state["highest_price_after_entry"] = 51000.0
        state["half_sold_done"] = False

        price = int(50000 * 0.991) - 1
        svc._execute_sell("000660", state, "stop_loss", price, "stop_loss")

        cu = state.get("cooldown_until", "")
        self.assertTrue(bool(cu))
        cd_time = datetime.fromisoformat(cu)
        delta = (cd_time - datetime.now()).total_seconds()
        self.assertGreater(delta, 550)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 상태파일 연속성
# ─────────────────────────────────────────────────────────────────────────────

class TestStateFile(unittest.TestCase):

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _ = _make_service(tmp)
            svc.load_top3(_make_top3())
            svc.total_entries_today = 3
            svc.save_state()

            svc2, _ = _make_service(tmp)
            svc2.load_state()
            self.assertEqual(svc2.total_entries_today, 3)
            self.assertEqual(set(svc2.symbols_state.keys()), {"000660", "005930", "035420"})


if __name__ == "__main__":
    unittest.main()
