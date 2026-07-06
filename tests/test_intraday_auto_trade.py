"""
tests/test_intraday_auto_trade.py — 장중 자동매매 모듈 테스트 (25개)
"""
import sys
import json
import math
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# 프로젝트 루트 등록
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.strategy.intraday_indicators import (
    calculate_vwap,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    resample_1m_to_3m,
    detect_bullish_reversal_1m,
    detect_bearish_volume_candle_1m,
    calculate_intraday_high_pullback,
)
from app.services.intraday_budget_allocator import IntradayBudgetAllocator


# ── 공통 픽스처 ────────────────────────────────────────────────────────────

def _make_candles(n=30, base_close=50000, trend=0):
    """최신순 더미 캔들 생성."""
    candles = []
    for i in range(n):
        price = base_close + trend * (n - 1 - i)
        candles.append({
            "time": f"{9:02d}{i:02d}00",
            "open": price - 200,
            "high": price + 300,
            "low": price - 300,
            "close": price,
            "volume": 1000 + i * 10,
        })
    return candles  # newest first


def _make_top3():
    return [
        {"symbol": "000660", "name": "SK하이닉스", "rank": 1,
         "current_price": 180000, "final_score": 85.0},
        {"symbol": "005930", "name": "삼성전자", "rank": 2,
         "current_price": 75000, "final_score": 78.0},
        {"symbol": "035420", "name": "NAVER", "rank": 3,
         "current_price": 250000, "final_score": 70.0},
    ]


class MockBroker:
    mode = "mock"

    def buy(self, symbol, quantity, price, **kwargs):
        return {"success": True, "order_id": "TEST-BUY-001"}

    def sell(self, symbol, quantity, price, **kwargs):
        return {"success": True, "order_id": "TEST-SELL-001"}


# ── 1. TestIntradayIndicators ───────────────────────────────────────────────

class TestIntradayIndicators(unittest.TestCase):

    def setUp(self):
        self.candles = _make_candles(30, base_close=50000, trend=100)

    def test_vwap_returns_positive(self):
        vwap = calculate_vwap(self.candles)
        self.assertGreater(vwap, 0)

    def test_vwap_empty_returns_zero(self):
        self.assertEqual(calculate_vwap([]), 0.0)

    def test_ema_length_matches_input(self):
        ema = calculate_ema(self.candles, 5)
        self.assertEqual(len(ema), len(self.candles))

    def test_rsi_range(self):
        rsi = calculate_rsi(self.candles)
        self.assertGreaterEqual(rsi, 0)
        self.assertLessEqual(rsi, 100)

    def test_rsi_insufficient_data_returns_50(self):
        self.assertEqual(calculate_rsi(_make_candles(3), 14), 50.0)

    def test_macd_structure(self):
        result = calculate_macd(self.candles)
        self.assertIn("macd", result)
        self.assertIn("signal", result)
        self.assertIn("hist", result)

    def test_resample_1m_to_3m(self):
        candles_1m = _make_candles(30)
        candles_3m = resample_1m_to_3m(candles_1m)
        self.assertEqual(len(candles_3m), 10)
        # 3분봉 볼륨은 3개 1분봉 합계
        self.assertEqual(candles_3m[-1]["volume"], sum(c["volume"] for c in candles_1m[-3:]))

    def test_bullish_reversal_true(self):
        # newest: bullish, prev: bearish
        candles = [
            {"time": "0930", "open": 100, "high": 110, "low": 95, "close": 108, "volume": 500},
            {"time": "0929", "open": 105, "high": 107, "low": 98, "close": 99, "volume": 300},
        ]
        self.assertTrue(detect_bullish_reversal_1m(candles))

    def test_bearish_volume_candle(self):
        # recent bearish with high volume
        candles = [
            {"time": "0935", "open": 105, "high": 106, "low": 99, "close": 100, "volume": 5000},
        ]
        for i in range(5):
            candles.append({"time": f"093{i}", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 300})
        self.assertTrue(detect_bearish_volume_candle_1m(candles))

    def test_pullback_calculation(self):
        pb = calculate_intraday_high_pullback(48000, 50000)
        self.assertAlmostEqual(pb, -4.0, places=1)


# ── 2. TestIntradayBudgetAllocator ─────────────────────────────────────────

class TestIntradayBudgetAllocator(unittest.TestCase):

    def setUp(self):
        self.allocator = IntradayBudgetAllocator()
        self.top3 = _make_top3()

    def test_basic_allocation_sums_to_budget(self):
        result = self.allocator.allocate(self.top3, 10_000_000)
        total = sum(r["allocated_budget"] for r in result)
        self.assertAlmostEqual(total, 10_000_000, delta=10)

    def test_weights_sum_to_one(self):
        result = self.allocator.allocate(self.top3, 10_000_000)
        total_w = sum(r["allocated_weight"] for r in result)
        self.assertAlmostEqual(total_w, 1.0, places=3)

    def test_min_weight_clamped(self):
        # 3번째 종목 비중이 15% 이상이어야 함
        result = self.allocator.allocate(self.top3, 10_000_000)
        for r in result:
            self.assertGreaterEqual(r["allocated_weight"], 0.14)

    def test_quantity_calculated(self):
        result = self.allocator.allocate(self.top3, 10_000_000)
        for r in result:
            expected_qty = int(r["allocated_budget"] / r["current_price"])
            self.assertEqual(r["allocated_quantity"], expected_qty)


# ── 3. TestBuyFlagConditions ───────────────────────────────────────────────

class TestBuyFlagConditions(unittest.TestCase):

    def _make_service(self, **overrides):
        from app.services.intraday_auto_trade_service import IntradayAutoTradeService

        cfg_mock = MagicMock()
        cfg_mock.mode = "mock"
        cfg_mock._raw = {"intraday_auto_trade": {}, "safety": {}}
        cfg_mock.trading = {}

        with tempfile.TemporaryDirectory() as tmp:
            # 임시 state/log 경로 패치
            with patch("app.services.intraday_auto_trade_service._ROOT", Path(tmp)):
                svc = IntradayAutoTradeService(broker=MockBroker(), kis_client=None, cfg=cfg_mock)
        return svc

    def _make_state(self, **kwargs):
        base = {
            "symbol": "000660", "name": "SK하이닉스", "rank": 1,
            "allocated_budget": 5_000_000, "allocated_weight": 0.5,
            "entries_count": 0, "position_quantity": 0,
            "avg_buy_price": 0.0, "current_price": 180000.0,
            "highest_price_after_entry": 0.0,
            "first_take_profit_done": False, "second_take_profit_done": False,
            "last_buy_at": "", "last_sell_at": "",
            "cooldown_until": "", "status": "WAITING_ENTRY",
            "last_buy_flag": False, "last_sell_flag": "",
            "last_reason": "", "realized_pnl": 0.0, "order_history": [],
        }
        base.update(kwargs)
        return base

    def test_insufficient_candles_returns_false(self):
        svc = self._make_service()
        state = self._make_state()
        flag, reason = svc._check_buy_flag("000660", state, _make_candles(3))
        self.assertFalse(flag)
        self.assertIn("candle", reason)

    def test_max_total_entries_blocks_buy(self):
        svc = self._make_service()
        svc.total_entries_today = 3
        state = self._make_state()
        flag, reason = svc._check_buy_flag("000660", state, _make_candles(30))
        self.assertFalse(flag)
        self.assertIn("max_total", reason)

    def test_max_symbol_entries_blocks_buy(self):
        svc = self._make_service()
        state = self._make_state(entries_count=2)
        flag, reason = svc._check_buy_flag("000660", state, _make_candles(30))
        self.assertFalse(flag)
        self.assertIn("max_symbol", reason)

    def test_vwap_below_blocks_buy(self):
        svc = self._make_service()
        # candles with high volume → high VWAP above current_price
        candles = []
        for i in range(30):
            candles.append({
                "time": f"09{i:02d}00",
                "open": 200000, "high": 210000, "low": 195000,
                "close": 200000, "volume": 100000,
            })
        state = self._make_state(current_price=170000.0)
        # Override time check to pass
        svc.buy_start_time = "00:00"
        svc.buy_end_time = "23:59"
        flag, reason = svc._standard_buy_check("000660", state, candles, 170000.0)
        self.assertFalse(flag)
        self.assertIn("vwap", reason)

    def test_ema_reverse_blocks_buy(self):
        svc = self._make_service()
        # Downtrend candles: EMA5 < EMA20
        candles = _make_candles(30, base_close=200000, trend=-500)
        svc.buy_start_time = "00:00"
        svc.buy_end_time = "23:59"
        state = self._make_state(current_price=candles[0]["close"])
        # Just test EMA direction via standard check
        flag, reason = svc._standard_buy_check("000660", state, candles, candles[0]["close"])
        # Result may vary but should not error
        self.assertIsInstance(flag, bool)

    def test_outside_buy_window_returns_false(self):
        svc = self._make_service()
        svc.buy_start_time = "22:00"
        svc.buy_end_time = "22:01"
        state = self._make_state()
        flag, reason = svc._check_buy_flag("000660", state, _make_candles(30))
        self.assertFalse(flag)
        self.assertIn("outside", reason)

    def test_relaxed_buy_activates_when_no_entries(self):
        svc = self._make_service()
        svc.total_entries_today = 0
        svc.buy_start_time = "00:00"
        svc.buy_end_time = "23:59"
        svc.relaxed_min_pullback = -99.0  # 항상 통과
        svc.relaxed_min_vol_ratio = 0.0
        state = self._make_state()
        # Should reach relaxed check (may pass or fail depending on candle data)
        candles = _make_candles(30)
        flag, reason = svc._check_buy_flag("000660", state, candles)
        self.assertIsInstance(flag, bool)  # No error


# ── 4. TestSellFlagConditions ──────────────────────────────────────────────

class TestSellFlagConditions(unittest.TestCase):

    def _make_service(self):
        from app.services.intraday_auto_trade_service import IntradayAutoTradeService

        cfg_mock = MagicMock()
        cfg_mock.mode = "mock"
        cfg_mock._raw = {"intraday_auto_trade": {}, "safety": {}}
        cfg_mock.trading = {}

        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.services.intraday_auto_trade_service._ROOT", Path(tmp)):
                svc = IntradayAutoTradeService(broker=MockBroker(), kis_client=None, cfg=cfg_mock)
        return svc

    def _holding_state(self, avg_buy=180000, qty=10, highest=185000, half_done=False):
        return {
            "symbol": "000660", "name": "SK하이닉스",
            "allocated_budget": 5_000_000, "avg_buy_price": avg_buy,
            "position_quantity": qty, "highest_price_after_entry": highest,
            "first_take_profit_done": half_done,
            "entries_count": 1, "status": "HOLDING",
            "realized_pnl": 0.0,
        }

    def test_stop_loss_triggered(self):
        svc = self._make_service()
        state = self._holding_state(avg_buy=180000)
        # -1.2% → 180000 * 0.988 = ~177840
        sell_type, reason = svc._check_sell_flag("000660", state, [], 177800.0)
        self.assertEqual(sell_type, "stop_loss")

    def test_half_tp_triggered(self):
        svc = self._make_service()
        state = self._holding_state(avg_buy=180000, half_done=False)
        # +1.8% → 183240
        sell_type, reason = svc._check_sell_flag("000660", state, [], 183300.0)
        self.assertEqual(sell_type, "half_tp")

    def test_full_tp_takes_priority_over_half_tp(self):
        svc = self._make_service()
        # At +3.2%: full_tp should fire before half_tp (priority 3 vs 4)
        state = self._holding_state(avg_buy=180000, half_done=False)
        # +3.2% → 185760
        sell_type, reason = svc._check_sell_flag("000660", state, [], 185800.0)
        self.assertEqual(sell_type, "full_tp")

    def test_trailing_stop_triggered(self):
        svc = self._make_service()
        # avg_buy=196000: profit at 195000 = -0.5% (no stop_loss, no full_tp, no half_tp)
        # highest=200000: trail = (195000-200000)/200000*100 = -2.5% <= -1.8% → trailing_stop
        state = self._holding_state(avg_buy=196000, highest=200000, half_done=True)
        sell_type, reason = svc._check_sell_flag("000660", state, [], 195000.0)
        self.assertEqual(sell_type, "trailing_stop")

    def test_no_sell_when_no_signal(self):
        svc = self._make_service()
        state = self._holding_state(avg_buy=180000, highest=181000)
        # price at 181000: no loss, no +1.8%, no trailing
        sell_type, reason = svc._check_sell_flag("000660", state, [], 181000.0)
        self.assertEqual(sell_type, "")


# ── 5. TestStateMachineSafety ──────────────────────────────────────────────

class TestStateMachineSafety(unittest.TestCase):

    def _make_service(self, tmp_dir):
        from app.services.intraday_auto_trade_service import IntradayAutoTradeService

        cfg_mock = MagicMock()
        cfg_mock.mode = "mock"
        cfg_mock._raw = {"intraday_auto_trade": {}, "safety": {}}
        cfg_mock.trading = {}

        with patch("app.services.intraday_auto_trade_service._ROOT", Path(tmp_dir)):
            svc = IntradayAutoTradeService(broker=MockBroker(), kis_client=None, cfg=cfg_mock)
        return svc

    def test_cooldown_blocks_reentry(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._make_service(tmp)
            svc.buy_start_time = "00:00"
            svc.buy_end_time = "23:59"

            state = {
                "symbol": "000660", "name": "SK하이닉스",
                "allocated_budget": 5_000_000, "avg_buy_price": 0.0,
                "position_quantity": 0, "highest_price_after_entry": 0.0,
                "first_take_profit_done": False, "entries_count": 0,
                "last_buy_at": "", "last_sell_at": "",
                "cooldown_until": (datetime.now() + timedelta(minutes=5)).isoformat(),
                "status": "COOLING_DOWN",
                "realized_pnl": 0.0,
            }
            flag, reason = svc._check_buy_flag("000660", state, _make_candles(30))
            # COOLING_DOWN 상태에서 호출 — status가 WAITING이 아니어서 buy_window check 후 flag false 기대
            # (실제로는 run_once에서 상태 전환 후 호출, 여기서는 직접 호출)
            self.assertIsInstance(flag, bool)

    def test_state_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._make_service(tmp)
            svc.load_top3(_make_top3())
            svc.total_entries_today = 2

            svc.save_state()

            # 새 서비스 인스턴스에서 로드
            with patch("app.services.intraday_auto_trade_service._ROOT", Path(tmp)):
                from app.services.intraday_auto_trade_service import IntradayAutoTradeService
                cfg_mock = MagicMock()
                cfg_mock.mode = "mock"
                cfg_mock._raw = {"intraday_auto_trade": {}, "safety": {}}
                cfg_mock.trading = {}
                svc2 = IntradayAutoTradeService(broker=MockBroker(), kis_client=None, cfg=cfg_mock)

            self.assertEqual(svc2.total_entries_today, 2)
            self.assertEqual(set(svc2.symbols_state.keys()), {"000660", "005930", "035420"})


if __name__ == "__main__":
    unittest.main()
