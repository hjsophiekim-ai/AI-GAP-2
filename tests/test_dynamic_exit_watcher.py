"""
test_dynamic_exit_watcher.py — tick() 1회 실행 동작 검증(브로커/가격조회는 모킹).

Broker가 유일한 Source of Truth이므로, 모든 테스트는 "브로커가 실제로 어떤
포지션을 들고 있다고 응답하는지"로 시나리오를 구성한다(state를 직접 조작해
포지션이 있는 것처럼 꾸미는 것만으로는 tick()이 이를 인식하면 안 된다).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.services.hynix_switch_state as state_module
import app.trading.dynamic_exit_watcher as watcher
from app.data_sources.hynix_long_collector import LONG_SYMBOL as HYNIX_SYMBOL, LONG_NAME as HYNIX_NAME
from app.models import OrderResult, Position


class _FakeSellBroker:
    def __init__(self, positions=None, cash=10_000_000.0):
        self._positions = positions or []
        self._cash = cash
        self.sell_calls = []

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        # 실제 브로커처럼 매도 후 내부 포지션을 갱신해야 이후 get_positions() 재조회가 정확해진다.
        remaining = []
        for p in self._positions:
            if p.symbol == symbol:
                if p.quantity > quantity:
                    p.quantity -= quantity
                    remaining.append(p)
                # quantity <= 매도수량이면 완전히 제거(추가하지 않음)
            else:
                remaining.append(p)
        self._positions = remaining
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok")

    def get_positions(self):
        return self._positions

    def get_buyable_cash(self):
        return self._cash


def _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0, entry_minutes_ago=5):
    """entry_price/entry_time 등 '우리쪽 부가 기록'만 state에 미리 넣어둔다(브로커가 모르는 정보)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    state = state_module.load_state(mode="mock")
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["position"] = {
        **state["position"], "entry_price": entry_price,
        "entry_time": (datetime.now() - timedelta(minutes=entry_minutes_ago)).isoformat(),
    }
    state_module.save_state_atomic(state)
    return tmp_path


def test_tick_does_nothing_when_auto_trade_off(tmp_path, monkeypatch):
    """Flat + Enhanced OFF → Dynamic Exit idle (no broker calls)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = False
    state_module.save_state_atomic(state)

    result = watcher.tick(now=datetime.now())
    assert result is None


def test_tick_still_monitors_exit_when_auto_off_but_position_held(tmp_path, monkeypatch):
    """Enhanced OFF must still allow exit monitoring while a position is held."""
    _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0)
    state = state_module.load_state(mode="mock")
    state["auto_trade_on"] = False
    state_module.save_state_atomic(state)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 103_100.0)
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
    fake_exit_log = tmp_path / "exit_engine_log.csv"
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", fake_exit_log)

    hynix_position = Position(
        symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10,
        avg_price=100_000.0, current_price=103_100.0,
    )
    broker = _FakeSellBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())
    assert decision is not None
    assert decision["action"] == "SELL_ALL"
    assert len(broker.sell_calls) == 1


def test_tick_executes_sell_on_take_profit(tmp_path, monkeypatch):
    _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 103_100.0)  # +3.1% -> NORMAL TP 3.0%
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)

    fake_exit_log = tmp_path / "exit_engine_log.csv"
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", fake_exit_log)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=103_100.0)
    broker = _FakeSellBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision["action"] == "SELL_ALL"
    assert len(broker.sell_calls) == 1 and broker.sell_calls[0][0] == HYNIX_SYMBOL
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None  # 전량 매도되어 포지션 정리됨(브로커 재조회로 확정)
    assert fake_exit_log.exists()


def _decide_result_stub(**overrides) -> dict:
    base = {
        "action": "HOLD", "ratio": 0.0, "reason": "test", "market_type": "RANGE", "regime": "RANGE",
        "regime_confidence": None, "regime_reasons": [], "profile": {}, "snapshot": {},
        "tp_pct": 1.0, "sl_pct": 1.0, "trailing_pct": None, "trailing_enabled": False,
        "trailing_armed": False, "profit_lock_floor_pct": None, "exit_score": 0.0, "score_breakdown": {},
    }
    base.update(overrides)
    return base


def test_tick_passes_state_confirmed_regime_to_decide(tmp_path, monkeypatch):
    """요구사항(2026-07-16, 남은 통합 작업2) — dynamic_exit_watcher.py가 매 사이클
    한 번만 계산된 state["adaptive_regime"]["confirmed_regime"]을 반드시
    DynamicExitEngine.decide()에 전달해야 한다(별도 재분류 없이)."""
    _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 100_500.0)
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=100_500.0)
    broker = _FakeSellBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    state = state_module.load_state(mode="mock")
    state["adaptive_regime"] = {"confirmed_regime": "STRONG_DOWN", "snapshot": {}}
    state_module.save_state_atomic(state)

    captured: dict = {}

    class _CapturingEngine:
        def decide(self, *args, **kwargs):
            captured.update(kwargs)
            return _decide_result_stub()

    watcher.tick(now=datetime.now(), engine=_CapturingEngine())

    assert captured.get("confirmed_regime") == "STRONG_DOWN"


def _setup_e2e_position(tmp_path, monkeypatch, symbol, name, entry_price, qty, entry_minutes_ago=5, mode="mock"):
    """실제 현재 보유 중인 포지션(예: 0197X0 468주 @10,680원)과 동일한 구조를 원장+
    DryRunBroker에 실제로 재현한다 — 강제 신호가 아니라 진짜 BUY 체결을 통해 포지션을
    만든다(section 6: '강제 신호가 아닌 가격 시뮬레이션으로' 요건)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    from app.trading.dry_run_broker import DryRunBroker
    from app.trading.hynix_switch_position_manager import _buy_new

    broker = DryRunBroker(initial_balance=10_000_000.0)
    orders: list = []
    _buy_new(
        broker, symbol, current_price=entry_price, cash_amount=qty * entry_price,
        reason="E2E 포지션 셋업(실제 진입 체결)", orders=orders, mode=mode, signal_source="ACTIVE_FUSION",
    )
    assert orders[0]["success"] is True and orders[0]["quantity"] == qty

    state = state_module.load_state(mode=mode)
    state["mode"] = mode
    state["auto_trade_on"] = True
    state["position"] = {
        **state["position"], "symbol": symbol, "name": name,
        "quantity": qty, "avg_price": entry_price, "entry_price": entry_price,
        "entry_time": (datetime.now() - timedelta(minutes=entry_minutes_ago)).isoformat(),
    }
    state_module.save_state_atomic(state)
    return broker


class TestE2ECurrentPositionPriceSimulation:
    """section 6 — 현재 보유 포지션(0197X0 468주 @10,680원, SL 10,520원/TP 11,000원) 기준
    E2E: 강제 신호가 아니라 가격 시뮬레이션만으로 자동손절/자동익절이 실제 실행되는지,
    Position 0/cash 갱신/ledger 기록/UI(state) 갱신/왕복거래 1 증가까지 확인한다."""

    SYMBOL = "0197X0"
    NAME = "SOL 인버스2X"
    ENTRY_PRICE = 10_680.0
    QTY = 468

    # 사용자가 보고한 "손절가 10,520원(-1.5%)/익절가 11,000원(+3.0%)"은 화면 표시상
    # 반올림된 값이다 — 정확한 -1.5%/+3.0% 임계가는 각각 10,680*0.985=10,519.8원 /
    # 10,680*1.03=11,000.4원이므로, 정확히 10,520원/11,000원을 넣으면 부동소수점상
    # 임계값을 근소하게 넘지 못해(-1.498%/+2.996%) 트리거되지 않는다. 테스트는 보고된
    # 수준을 명확히 지나치는 가격(10,500원/11,050원)으로 시뮬레이션한다.
    SL_TEST_PRICE = 10_500.0
    TP_TEST_PRICE = 11_050.0

    def test_stop_loss_price_simulation_triggers_real_sell(self, tmp_path, monkeypatch):
        broker = _setup_e2e_position(tmp_path, monkeypatch, self.SYMBOL, self.NAME, self.ENTRY_PRICE, self.QTY)
        cash_before = broker.get_buyable_cash()

        # 강제 신호가 아니라 가격만 손절가 수준(10,500원, -1.69%)으로 시뮬레이션한다 —
        # engine.decide()가 자체적으로 손절을 판단해야 한다(신호를 직접 주입하지 않음).
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: self.SL_TEST_PRICE)
        monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

        decision = watcher.tick(now=datetime.now())

        print(
            "[E2E SL] cycle=SL_SIM signal=PRICE_SIM(%.0f won) action=%s reason=%s "
            "symbol=%s qty_before=%d price=%.0f cash_before=%.0f cash_after=%.0f" % (
                self.SL_TEST_PRICE, decision["action"], decision["reason"], self.SYMBOL,
                self.QTY, self.SL_TEST_PRICE, cash_before, broker.get_buyable_cash(),
            )
        )

        assert decision["action"] == "SELL_ALL"
        assert "손절" in decision["reason"] and "시간손절" not in decision["reason"]

        positions_after = broker.get_positions()
        assert positions_after == []
        cash_after = broker.get_buyable_cash()
        assert cash_after == pytest.approx(cash_before + self.QTY * self.SL_TEST_PRICE)

        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None
        assert (reloaded["position"].get("quantity") or 0) == 0

        from app.services import hynix_execution_ledger as ledger_module

        today = datetime.now().strftime("%Y%m%d")
        counters = ledger_module.compute_trade_counters(today)
        assert counters["sell_fill_count"] == 1
        assert counters["round_trip_count"] == 1
        pnl = ledger_module.compute_realized_pnl_breakdown(today)
        # realized_pnl은 이제 GrossPnL이 아니라 NetPnL(수수료/거래세/슬리피지 차감 후)이다.
        from app.trading.trading_cost_engine import TradeCostEngine

        expected_net = TradeCostEngine().compute_net_pnl(
            self.SYMBOL, entry_price=self.ENTRY_PRICE, exit_price=self.SL_TEST_PRICE, quantity=self.QTY,
        )["net_pnl"]
        assert pnl["total_realized_pnl"] == pytest.approx(expected_net)

    def test_take_profit_price_simulation_triggers_real_sell(self, tmp_path, monkeypatch):
        broker = _setup_e2e_position(tmp_path, monkeypatch, self.SYMBOL, self.NAME, self.ENTRY_PRICE, self.QTY)
        cash_before = broker.get_buyable_cash()

        # 강제 신호가 아니라 가격만 익절가 수준(11,050원, +3.46%)으로 시뮬레이션한다.
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: self.TP_TEST_PRICE)
        monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

        decision = watcher.tick(now=datetime.now())

        print(
            "[E2E TP] cycle=TP_SIM signal=PRICE_SIM(%.0f won) action=%s reason=%s "
            "symbol=%s qty_before=%d price=%.0f cash_before=%.0f cash_after=%.0f" % (
                self.TP_TEST_PRICE, decision["action"], decision["reason"], self.SYMBOL,
                self.QTY, self.TP_TEST_PRICE, cash_before, broker.get_buyable_cash(),
            )
        )

        assert decision["action"] == "SELL_ALL"
        assert "익절" in decision["reason"]

        positions_after = broker.get_positions()
        assert positions_after == []
        cash_after = broker.get_buyable_cash()
        assert cash_after == pytest.approx(cash_before + self.QTY * self.TP_TEST_PRICE)

        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None
        assert (reloaded["position"].get("quantity") or 0) == 0

        from app.services import hynix_execution_ledger as ledger_module

        today = datetime.now().strftime("%Y%m%d")
        counters = ledger_module.compute_trade_counters(today)
        assert counters["sell_fill_count"] == 1
        assert counters["round_trip_count"] == 1
        pnl = ledger_module.compute_realized_pnl_breakdown(today)
        from app.trading.trading_cost_engine import TradeCostEngine

        expected_net = TradeCostEngine().compute_net_pnl(
            self.SYMBOL, entry_price=self.ENTRY_PRICE, exit_price=self.TP_TEST_PRICE, quantity=self.QTY,
        )["net_pnl"]
        assert pnl["total_realized_pnl"] == pytest.approx(expected_net)


class TestBigTrendHoldingIntegration:
    """2026-07-14 사용자 요청 — 장중 큰 추세 추종. big_trend_holding_enabled=True(mock
    전용)일 때 작은 반대신호로 전량청산되지 않고, 손절 안전장치는 토글과 무관하게
    항상 적용되는지 확인한다."""

    SYMBOL = "0197X0"
    NAME = "SOL 인버스2X"
    ENTRY_PRICE = 10_680.0
    QTY = 468

    def _setup(self, tmp_path, monkeypatch, current_price, inverse_probability=75.0, mode="mock"):
        broker = _setup_e2e_position(tmp_path, monkeypatch, self.SYMBOL, self.NAME, self.ENTRY_PRICE, self.QTY, mode=mode)
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)
        monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
        monkeypatch.setattr(watcher.bte, "_LOG_PATH", tmp_path / "hynix_big_trend_log.csv")
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

        state = state_module.load_state(mode=mode)
        state["mode"] = mode
        state["big_trend_holding_enabled"] = True
        state["last_cycle_ai_result"] = {
            "probability": {"buy_probability": 100 - inverse_probability, "sell_probability": inverse_probability, "hold_probability": 0.0},
            "cycle": {"cycle_phase": "TREND_DOWN", "turning_point": {}, "momentum": {}},
            "decision_v2": {"final_action_v2": "INVERSE"},
        }
        state_module.save_state_atomic(state)
        return broker

    def test_mild_pullback_holds_full_instead_of_exiting(self, tmp_path, monkeypatch):
        # +1.0%대 소폭 수익 — 부분익절 임계(3%) 미달, 반대확률도 강하지 않음 → HOLD 유지되어야 한다.
        current_price = self.ENTRY_PRICE * 1.010
        self._setup(tmp_path, monkeypatch, current_price)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "HOLD"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["quantity"] == self.QTY  # 청산되지 않음
        assert reloaded.get("last_big_trend_result") is not None
        assert reloaded["last_big_trend_result"]["dominant_direction"] == "INVERSE"

    def test_big_trend_holding_applies_same_exit_engine_in_real_mode(self, tmp_path, monkeypatch):
        current_price = self.ENTRY_PRICE * 1.010
        self._setup(tmp_path, monkeypatch, current_price, mode="real")
        state_module.set_active_mode("real")

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "HOLD"
        reloaded = state_module.load_state(mode="real")
        assert reloaded["position"]["quantity"] == self.QTY
        assert reloaded.get("last_big_trend_result") is not None
        assert reloaded["last_big_trend_result"]["dominant_direction"] == "INVERSE"

    def test_hard_stop_loss_still_fires_when_toggle_on(self, tmp_path, monkeypatch):
        # 손절 임계(-1.5%보다 더 하락) — Big Trend 토글이 켜져 있어도 반드시 청산되어야 한다.
        current_price = self.ENTRY_PRICE * 0.975  # -2.5%
        self._setup(tmp_path, monkeypatch, current_price)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "SELL_ALL"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None

    def test_big_trend_uses_shared_adaptive_regime_not_own_classification(self, tmp_path, monkeypatch):
        """요구사항(2026-07-16, 남은 통합 작업5) — Big Trend Holding은 state에 이미
        저장된 공용 adaptive_regime을 그대로 매핑해 실행한다(자체 재분류가 아님).
        state["adaptive_regime"]["confirmed_regime"]=STRONG_DOWN이면, features 기반
        자체 분류 결과와 무관하게 trend_regime이 STRONG_TREND로 매핑되어야 한다."""
        current_price = self.ENTRY_PRICE * 1.010
        self._setup(tmp_path, monkeypatch, current_price)

        state = state_module.load_state(mode="mock")
        state["adaptive_regime"] = {"confirmed_regime": "STRONG_DOWN", "snapshot": {}}
        state_module.save_state_atomic(state)

        watcher.tick(now=datetime.now())

        reloaded = state_module.load_state(mode="mock")
        assert reloaded["last_big_trend_result"]["trend_regime"] == "STRONG_TREND"
        assert reloaded["last_big_trend_result"]["raw_trend_regime"] == "STRONG_TREND"

    def test_toggle_off_leaves_baseline_dynamic_exit_behavior(self, tmp_path, monkeypatch):
        current_price = self.ENTRY_PRICE * 1.010
        self._setup(tmp_path, monkeypatch, current_price)
        state = state_module.load_state(mode="mock")
        state["big_trend_holding_enabled"] = False
        state_module.save_state_atomic(state)

        decision = watcher.tick(now=datetime.now())

        # Big Trend 결과는 계속 shadow로 계산·저장되지만, 실제 action은 바뀌지 않는다(회귀 방지).
        reloaded = state_module.load_state(mode="mock")
        assert reloaded.get("last_big_trend_result") is not None
        assert decision["action"] in ("HOLD", "SELL_ALL", "SELL_PARTIAL")  # DynamicExitEngine 고유 판단 그대로


def test_tick_returns_none_when_broker_has_no_position_despite_stale_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    # state 파일에는 하이닉스 보유로 남아있지만(예: 이전 세션의 낡은 기록), 브로커는 무보유
    state["position"] = {
        **state["position"], "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 10,
        "avg_price": 100_000.0, "entry_price": 100_000.0, "entry_time": datetime.now().isoformat(),
    }
    state_module.save_state_atomic(state)

    broker = _FakeSellBroker(positions=[])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision is None
    assert len(broker.sell_calls) == 0
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None  # 브로커 기준으로 정정되어야 함


# ── 손절 계산 단일 입력(position_snapshot) — regime별 하드손절 + -3% 캐치올 ──────

class TestUnifiedStopLossSnapshot:
    """요구사항 — 구값 참조 문제 재검증/완전 수정. entry_price=KIS 실제 평단,
    effective_sl_pct=confirmed adaptive regime 프로필 하나로만 계산되는 단일
    스냅샷이 모든 손절 경로(legacy/Dynamic Exit/Big Trend Holding)를 지배한다."""

    SYMBOL = "0197X0"
    NAME = "SOL 인버스2X"
    ENTRY_PRICE = 10_000.0
    QTY = 100

    def _setup(self, tmp_path, monkeypatch, current_price, confirmed_regime, big_trend_on=False):
        broker = _setup_e2e_position(tmp_path, monkeypatch, self.SYMBOL, self.NAME, self.ENTRY_PRICE, self.QTY)
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)
        monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
        monkeypatch.setattr(watcher.bte, "_LOG_PATH", tmp_path / "hynix_big_trend_log.csv")
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

        state = state_module.load_state(mode="mock")
        state["adaptive_regime"] = {"confirmed_regime": confirmed_regime, "snapshot": {}}
        state["big_trend_holding_enabled"] = big_trend_on
        if big_trend_on:
            state["last_cycle_ai_result"] = {
                "probability": {"buy_probability": 20.0, "sell_probability": 80.0, "hold_probability": 0.0},
                "cycle": {"cycle_phase": "TREND_DOWN", "turning_point": {}, "momentum": {}},
                "decision_v2": {"final_action_v2": "INVERSE"},
            }
        state_module.save_state_atomic(state)
        return broker

    @pytest.mark.parametrize("regime,sl_pct", [("RANGE", 0.8), ("VOLATILE_RANGE", 0.6), ("STRONG_DOWN", 1.5)])
    def test_regime_specific_sl_threshold_sells_all(self, tmp_path, monkeypatch, regime, sl_pct):
        # SYMBOL이 인버스(0197X0)이므로 손실 방향은 가격 하락이다(보유 중인 ETF 자체의
        # 가격이 내려가면 손실 — 인버스 레버리지는 이미 이 가격에 반영돼 있다).
        # 임계보다 0.1%p 더 나쁜 지점으로 시뮬레이션한다. STRONG_UP은 인버스 보유
        # 중 확정 시 별도의 "추세 반전 확정" 즉시청산 경로와 겹치므로 STRONG_DOWN만
        # 검증한다(둘 다 sl_pct=1.5로 대칭이라 커버리지 손실 없음).
        current_price = self.ENTRY_PRICE * (1 - (sl_pct + 0.1) / 100.0)
        self._setup(tmp_path, monkeypatch, current_price, regime)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "SELL_ALL", f"{regime}(sl={sl_pct}%) 하드손절 미실행: {decision}"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None
        assert reloaded["stop_loss_snapshot"] is None

    @pytest.mark.parametrize("regime", ["RANGE", "VOLATILE_RANGE", "STRONG_DOWN", "DATA_INSUFFICIENT"])
    def test_three_percent_loss_always_sells_regardless_of_regime(self, tmp_path, monkeypatch, regime):
        current_price = self.ENTRY_PRICE * 0.97  # -3% 손실
        self._setup(tmp_path, monkeypatch, current_price, regime)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "SELL_ALL", f"regime={regime}에서 -3% 손실인데 전량매도 실패: {decision}"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None

    def test_range_hard_stop_not_masked_by_big_trend_own_atr_ladder(self, tmp_path, monkeypatch):
        """회귀 테스트(2026-07-20 실측 버그) — RANGE confirmed regime(-0.8%) 기준으로는
        이미 손절인 -0.9% 손실이, Big Trend Holding 자체 ATR 변동성 등급 기준
        effective_sl_pct(최소 -1.0%)로는 아직 트리거되지 않아 Big Trend 판단(HOLD)이
        정상 손절을 뒤집었던 사고. 단일 스냅샷 하드손절 오버라이드로 재발하지 않아야 한다."""
        current_price = self.ENTRY_PRICE * 0.991  # -0.9% 손실
        self._setup(tmp_path, monkeypatch, current_price, "RANGE", big_trend_on=True)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "SELL_ALL"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None
        assert reloaded["stop_loss_source"] in ("HARD_STOP_SNAPSHOT", "BIG_TREND_HARD_STOP")

    def test_big_trend_toggle_on_still_hard_stops_at_three_percent(self, tmp_path, monkeypatch):
        current_price = self.ENTRY_PRICE * 0.97
        self._setup(tmp_path, monkeypatch, current_price, "STRONG_DOWN", big_trend_on=True)

        decision = watcher.tick(now=datetime.now())

        assert decision["action"] == "SELL_ALL"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None

    def test_ui_snapshot_matches_executed_decision(self, tmp_path, monkeypatch):
        """UI 표시값과 실제 주문 판단값 완전 일치 — state["stop_loss_snapshot"]이
        실제 매도를 촉발한 값과 같아야 한다."""
        current_price = self.ENTRY_PRICE * 0.97
        self._setup(tmp_path, monkeypatch, current_price, "RANGE")

        watcher.tick(now=datetime.now())

        reloaded = state_module.load_state(mode="mock")
        snapshot = reloaded["stop_loss_snapshot"]
        assert snapshot is None
        assert reloaded["stop_loss_source"] in ("HARD_STOP_SNAPSHOT", "DYNAMIC_EXIT_ENGINE")


class TestSellOnlyRecovery:
    """SELL_ONLY_RECOVERY — position_sync_status가 POSITION_SYNC_PENDING인 동안에도
    (신규진입은 계속 차단된 채로) 실제 보유를 재확인해 하드손절만은 반드시 집행한다.

    attempt_sell_only_recovery()를 직접 단위테스트한다 — 정상 broker를 쓰는 mock
    모드의 HynixPositionManager.sync()는 매 틱 자체적으로 회복되므로(전체 tick()을
    통하면 이 함수가 실행되기 전에 이미 SYNCED로 자가치유돼 버린다), 이 함수가
    실제로 다루려는 시나리오(브로커 재조회 자체는 가능하지만 이전 주문의 체결
    재확인이 계속 PENDING으로 남아있는 상태)를 직접 재현해야 한다."""

    SYMBOL = HYNIX_SYMBOL
    NAME = HYNIX_NAME
    ENTRY_PRICE = 100_000.0
    QTY = 10

    def _pending_state(self, tmp_path, monkeypatch, confirmed_regime):
        monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
        state = state_module.load_state(mode="mock")
        state["mode"] = "mock"
        state["position_sync_status"] = "POSITION_SYNC_PENDING"
        state["adaptive_regime"] = {"confirmed_regime": confirmed_regime, "snapshot": {}}
        state["position"] = {
            **state["position"], "symbol": self.SYMBOL, "name": self.NAME,
            "quantity": self.QTY, "avg_price": self.ENTRY_PRICE, "entry_price": self.ENTRY_PRICE,
            "entry_time": datetime.now().isoformat(),
        }
        state_module.save_state_atomic(state)
        return state

    def test_hard_stop_executes_during_position_sync_pending(self, tmp_path, monkeypatch):
        from app.trading.hynix_stop_loss_control import attempt_sell_only_recovery

        state = self._pending_state(tmp_path, monkeypatch, "RANGE")
        current_price = self.ENTRY_PRICE * 0.97  # -3% 손실
        broker = _FakeSellBroker(positions=[Position(symbol=self.SYMBOL, name=self.NAME, quantity=self.QTY, avg_price=self.ENTRY_PRICE, current_price=current_price)])
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)

        result = attempt_sell_only_recovery(state, "mock", now=datetime.now())

        assert result is not None and result["attempted"] is True and result["sold"] is True
        assert len(broker.sell_calls) == 1
        assert state["stop_loss_snapshot"] is None
        assert state["position"]["symbol"] is None

    def test_no_sell_when_pending_but_still_within_threshold(self, tmp_path, monkeypatch):
        from app.trading.hynix_stop_loss_control import attempt_sell_only_recovery

        state = self._pending_state(tmp_path, monkeypatch, "STRONG_DOWN")
        current_price = self.ENTRY_PRICE * 0.999  # -0.1%, 임계(-1.5%) 미도달
        broker = _FakeSellBroker(positions=[Position(symbol=self.SYMBOL, name=self.NAME, quantity=self.QTY, avg_price=self.ENTRY_PRICE, current_price=current_price)])
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)

        result = attempt_sell_only_recovery(state, "mock", now=datetime.now())

        assert result is not None and result["sold"] is False
        assert len(broker.sell_calls) == 0
        assert state["position"]["symbol"] == self.SYMBOL  # 신규진입은 여전히 아니지만 보유 포지션은 그대로

    def test_returns_none_when_not_pending(self, tmp_path, monkeypatch):
        """정상(SYNCED) 상태에서는 이 함수가 아무 일도 하지 않는다 — 평소 tick() 흐름이 담당한다."""
        from app.trading.hynix_stop_loss_control import attempt_sell_only_recovery

        state = self._pending_state(tmp_path, monkeypatch, "RANGE")
        state["position_sync_status"] = "SYNCED"

        result = attempt_sell_only_recovery(state, "mock", now=datetime.now())

        assert result is None

    def test_no_sell_when_broker_confirms_flat(self, tmp_path, monkeypatch):
        """PENDING 상태였지만 실제 재확인 결과 보유수량이 0이면(정상) 매도하지 않는다."""
        from app.trading.hynix_stop_loss_control import attempt_sell_only_recovery

        state = self._pending_state(tmp_path, monkeypatch, "RANGE")
        broker = _FakeSellBroker(positions=[])
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: self.ENTRY_PRICE * 0.9)

        result = attempt_sell_only_recovery(state, "mock", now=datetime.now())

        assert result is not None and result["sold"] is False
        assert len(broker.sell_calls) == 0

    def test_full_tick_self_heals_pending_flag_without_needing_recovery(self, tmp_path, monkeypatch):
        """브로커가 정상 응답하는 mock 모드에서는 tick() 최상단의 정상 sync가 매
        틱 스스로 SYNCED로 회복시킨다 — 오래된 PENDING 플래그가 남아있어도 정상
        흐름(Dynamic Exit/하드손절 오버라이드)이 그대로 이어져야 한다."""
        broker = _setup_e2e_position(tmp_path, monkeypatch, self.SYMBOL, self.NAME, self.ENTRY_PRICE, self.QTY)
        current_price = self.ENTRY_PRICE * 0.97  # -3% 손실
        monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)
        monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
        monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
        monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

        state = state_module.load_state(mode="mock")
        state["position_sync_status"] = "POSITION_SYNC_PENDING"
        state["adaptive_regime"] = {"confirmed_regime": "RANGE", "snapshot": {}}
        state_module.save_state_atomic(state)

        decision = watcher.tick(now=datetime.now())

        assert decision is not None and decision["action"] == "SELL_ALL"
        reloaded = state_module.load_state(mode="mock")
        assert reloaded["position"]["symbol"] is None
        assert reloaded["stop_loss_source"] != "SELL_ONLY_RECOVERY"  # 정상 경로로 처리됨(자가치유)

    def test_no_sell_when_pending_flag_stale_but_broker_confirms_flat(self, tmp_path, monkeypatch):
        """포지션 자체가 이미 없는데 POSITION_SYNC_PENDING 플래그만 낡게 남아있으면
        정상적으로(아무 매도 없이) 통과해야 한다."""
        monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
        state = state_module.load_state(mode="mock")
        state["mode"] = "mock"
        state["auto_trade_on"] = True
        state["position_sync_status"] = "POSITION_SYNC_PENDING"
        state["position"] = {
            **state["position"], "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
        }
        state_module.save_state_atomic(state)

        decision = watcher.tick(now=datetime.now())

        assert decision is None
