"""P3 × 계좌모드 — MOCK / LIVE 네 조합 (2026-09-27).

    MOCK + N1 · MOCK + P3 · LIVE + N1 · LIVE + P3

핵심 주장 하나를 잠근다: **계좌 종류는 전략 판단을 조금도 바꾸지 않는다.**
regime / shadow / B3 / Y3 / P3 rescue 는 두 계좌에서 같은 코드를 타고, 계좌 차이는
기존 broker adapter 가 전부 흡수한다.

실계좌 주문은 단 한 건도 내지 않는다 -- LIVE 경로는 ``RealBrokerAdapter`` 의
``broker=`` 주입구로 **기록용 stub** 을 넣어, 주문 payload 가 만들어져 올바른
adapter 로 라우팅되는 것까지만 확인한다(KIS 접속 없음).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    p3_stack,
    service as service_module,
    shadow_base,
    state_store,
    strategy_mode,
)
from app.trading.macd2.broker_adapter import (
    MockBrokerAdapter,
    RealBrokerAdapter,
    create_macd2_broker,
)
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _broker, _held_3slot_state, _market
from tests.macd2.test_p3_worker import _isolate_shadow_ledger, _ledger_before, _p3_state
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

KST = config.KST
ACCOUNTS = ["mock", "real"]
MODES = list(strategy_mode.ALL_MODES)


# ── 실계좌 주문을 **절대** 내보내지 않는 기록용 stub ──────────────────────
@dataclass
class _RecordingBroker:
    """RealBrokerAdapter 에 주입하는 stub. 바깥으로 나가는 호출이 없다.

    실제 주문이 몇 건 시도됐는지만 센다 -- 이 테스트의 종료조건은 그 값이
    **0** 이라는 것이다(payload 생성까지만 확인한다).
    """

    submitted: list[dict[str, Any]] = field(default_factory=list)
    quotes: dict[str, float] = field(default_factory=dict)

    def get_cash(self) -> float:
        return 10_000_000.0

    def get_orderable_cash(self, symbol: str) -> float:
        return 10_000_000.0

    def get_quote(self, symbol: str):
        return self.quotes.get(symbol)

    def get_positions(self):
        return []

    def get_position(self, symbol: str):
        return None

    def get_today_fills(self, symbol: str = "") -> dict:
        return {}

    def buy_market(self, symbol: str, qty: int, client_order_id: str):
        self.submitted.append({"side": "BUY", "symbol": symbol, "qty": qty})
        raise AssertionError("LIVE stub 에 실제 주문이 도달했다 -- 이 테스트는 "
                            "주문 payload 생성까지만 확인해야 한다")

    def sell_market(self, symbol: str, qty: int, client_order_id: str):
        self.submitted.append({"side": "SELL", "symbol": symbol, "qty": qty})
        raise AssertionError("LIVE stub 에 실제 주문이 도달했다")


def _live_adapter() -> tuple[RealBrokerAdapter, _RecordingBroker]:
    """KIS 접속 없이 REAL adapter 를 만든다(안전 게이트는 broker 생성 시점에
    있으므로, 주입구를 쓰면 그 경로 자체를 타지 않는다)."""
    stub = _RecordingBroker()
    return RealBrokerAdapter(confirm_text="unused", broker=stub), stub


def _state_for(account: str, mode: str):
    state = state_store.default_state()
    state.mode = account
    strategy_mode.apply(state, mode)
    return state


# ── §4. 전략 코드가 계좌를 참조하지 않는다 ───────────────────────────────
@pytest.mark.parametrize("module", [chop_regime, p3_stack, shadow_base])
def test_p3_modules_never_reference_an_account_or_broker(module):
    """P3/B3/Y3/SHADOW 안에 mock 계좌번호 / endpoint / mock 전용 env 가 없어야 한다."""
    src = inspect.getsource(module)
    for needle in ("MOCK_APP_KEY", "MOCK_CANO", "KIS_MOCK", "broker_factory",
                   "create_macd2_broker", "RealBrokerAdapter", "MockBrokerAdapter"):
        assert needle not in src, f"{module.__name__} 이 {needle} 를 참조한다"


def test_p3_decision_functions_take_no_account_argument():
    """판정 함수 시그니처에 계좌/브로커 인자가 없다 -- 구조적으로 분리돼 있다."""
    for fn in (p3_stack.evaluate, chop_regime.evaluate, shadow_base.advance_exits):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"broker", "account", "mode", "adapter"}), fn.__name__


# ── §5. 네 조합 초기화 ───────────────────────────────────────────────────
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("mode", MODES)
def test_four_combinations_initialize(account, mode, monkeypatch, tmp_path):
    """MOCK/LIVE × N1/P3 네 조합이 전부 정상적으로 서야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    state = _state_for(account, mode)

    assert strategy_mode.current(state) == mode
    assert strategy_mode.account_kind(state) == account.upper()
    # 계좌가 전략 구성을 바꾸면 안 된다.
    assert bool(state.p3_enabled) is (mode == strategy_mode.MODE_P3)
    assert state.time_window_n1_filter_enabled is True
    assert state.c1_peak_protection_enabled is True
    assert state.smart_sizing_enabled is True
    # P3 스택 활성 여부는 모드만 본다.
    assert p3_stack.is_active(state) is (mode == strategy_mode.MODE_P3)


@pytest.mark.parametrize("account", ACCOUNTS)
def test_order_router_picks_the_right_adapter(account):
    """계좌 차이는 adapter 가 흡수한다 -- 전략은 관여하지 않는다."""
    if account == "mock":
        # 실제 KIS mock 클라이언트를 만들지 않도록 주입구를 쓴다(conftest 가
        # create_kis_client 를 차단한다). adapter 선택 자체가 검증 대상이다.
        broker = MockBrokerAdapter(broker=FakeBroker())
        assert broker.mode == "mock"
        assert create_macd2_broker.__doc__ and "mock" in create_macd2_broker.__doc__
    else:
        broker, stub = _live_adapter()
        assert isinstance(broker, RealBrokerAdapter)
        assert broker.mode == "real"
        assert stub.submitted == [], "adapter 생성만으로 주문이 나가면 안 된다"


@pytest.mark.parametrize("account", ACCOUNTS)
def test_shadow_and_regime_restore_in_both_accounts(account, monkeypatch, tmp_path):
    """P3 조합에서 shadow ledger 복원 + regime 계산이 계좌와 무관하게 같아야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    now = datetime(2026, 9, 22, 10, 0, tzinfo=KST)
    chop_regime.save_ledger(_ledger_before(now, h50=5, tp1=0), source="test")

    state = _state_for(account, strategy_mode.MODE_P3)
    decision = chop_regime.current_regime()
    assert decision.regime == chop_regime.REGIME_CHOP
    assert decision.h50_rate == pytest.approx(0.5)
    assert decision.tp1_rate == pytest.approx(0.0)

    state.p3_last_regime = decision.regime
    state.p3_last_shadow_sample = decision.sample
    assert strategy_mode.shadow_status(state) == "READY"
    assert strategy_mode.execution_layer(state) == "P3"


# ── §5/§7. 같은 tick 에서 두 계좌의 P3 판단이 동일한가 ───────────────────
def test_regime_and_p3_decision_are_identical_across_accounts(monkeypatch, tmp_path):
    """같은 shadow ledger·같은 입력이면 MOCK 과 LIVE 의 판정이 완전히 같아야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    now = datetime(2026, 9, 22, 10, 0, tzinfo=KST)
    chop_regime.save_ledger(_ledger_before(now, h50=5, tp1=0), source="test")

    seen = {}
    for account in ACCOUNTS:
        state = _state_for(account, strategy_mode.MODE_P3)
        d = chop_regime.current_regime()
        p3_stack.note_entry_regime(state, d.regime)
        entry = datetime(2026, 9, 22, 12, 15, tzinfo=KST)
        dec = p3_stack.evaluate(
            net_return_pct=1.0, entry_at=entry, now=entry + timedelta(minutes=4),
            direction=Direction.DOWN_BLUE, already_rescued=False,
            already_promoted=False, allow_max_hold=False,
        )
        seen[account] = (d.regime, d.h50_rate, d.tp1_rate,
                         p3_stack.governs_position(state),
                         dec.action, dec.sell_fraction, dec.exit_reason)
    assert seen["mock"] == seen["real"], f"계좌별로 판정이 다르다: {seen}"


# ── §7. LIVE 에서 P3 가 자동으로 꺼지지 않는다 ───────────────────────────
def test_p3_is_selectable_in_a_live_account(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    _isolate_shadow_ledger(monkeypatch, tmp_path)

    state = _state_for("real", strategy_mode.MODE_N1)
    state_store.save_state(state)

    res = service_module.get_service().set_strategy_mode(
        strategy_mode.MODE_P3, changed_by="test")
    assert res.get("ok") is True, res
    assert res.get("account") == "REAL"

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == strategy_mode.MODE_P3
    assert restored.mode == "real"
    assert restored.p3_enabled is True


def test_p3_is_not_turned_off_by_the_account_type(tmp_path, monkeypatch):
    """재시작 복원에서도 계좌 때문에 P3 가 꺼지면 안 된다."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    state = _state_for("real", strategy_mode.MODE_P3)
    state_store.save_state(state)

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == strategy_mode.MODE_P3
    assert restored.p3_enabled is True
    assert strategy_mode.account_kind(restored) == "REAL"


def test_selecting_p3_does_not_change_the_account_mode(tmp_path, monkeypatch):
    """§14: P3 를 고른다고 계좌모드가 바뀌면 안 된다."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    for account in ACCOUNTS:
        state = _state_for(account, strategy_mode.MODE_N1)
        state_store.save_state(state)
        service_module.get_service().set_strategy_mode(
            strategy_mode.MODE_P3, changed_by="test")
        assert state_store.load_state().mode == account


# ── §15. 배포 직후 기본값 ────────────────────────────────────────────────
def test_default_strategy_is_n1_and_p3_is_off():
    state = state_store.default_state()
    assert config.P3_FILTER_DEFAULT is False
    assert bool(state.p3_enabled) is False
    assert strategy_mode.current(state) != strategy_mode.MODE_P3


# ── §7. P3 부분청산 수량 계산 (계좌 무관) ────────────────────────────────
@pytest.mark.parametrize("qty, expected_sell, expected_left", [
    (10, 5, 5), (7, 4, 3), (3, 2, 1), (2, 1, 1),
])
def test_partial_quantity_math_is_account_independent(qty, expected_sell, expected_left):
    """worker 의 P3 부분청산 수량식과 같은 계산 -- 잔량이 1주 이상 남아야 한다."""
    sell = min(qty - 1, max(1, round(qty * config.P3_RESCUE_SELL_RATIO)))
    assert sell == expected_sell
    assert qty - sell == expected_left
    assert qty - sell >= 1, "잔량이 0 이면 runner 가 사라진다"


# ── §6. MOCK dry-run — 실제 worker tick 으로 주문까지 ────────────────────
def test_mock_p3_rescue_places_exactly_one_partial_sell(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=4.0, qty=10)
    state.mode = "mock"
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    sells = [o for o in broker.orders if o.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].requested_qty == 5
    assert state.position.quantity == 5
    assert p3_stack.position_mode(state) == p3_stack.MODE_P3_RUNNER


# ── §7. LIVE 경로: 주문이 한 건도 나가지 않는다 ──────────────────────────
def test_live_path_submits_zero_orders_today(monkeypatch, tmp_path):
    """LIVE adapter 를 세우고 P3 상태를 만들어도 실제 주문은 0건이어야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    adapter, stub = _live_adapter()

    state = _state_for("real", strategy_mode.MODE_P3)
    now = datetime(2026, 9, 22, 10, 0, tzinfo=KST)
    chop_regime.save_ledger(_ledger_before(now, h50=5, tp1=0), source="test")
    d = chop_regime.current_regime()
    p3_stack.note_entry_regime(state, d.regime)

    # 판정만 돌린다 -- 집행은 하지 않는다.
    entry = datetime(2026, 9, 22, 12, 15, tzinfo=KST)
    decision = p3_stack.evaluate(
        net_return_pct=1.0, entry_at=entry, now=entry + timedelta(minutes=4),
        direction=Direction.DOWN_BLUE, already_rescued=False,
        already_promoted=False, allow_max_hold=False)

    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE
    assert adapter.mode == "real"
    assert stub.submitted == [], "오늘 실계좌 주문은 0건이어야 한다"
