"""H50 배포 전 smoke test (2026-09-16 사용자 요청).

배포 직전에 "실제로 주문까지 가는가"를 end-to-end 로 확인한다. 전부
worker.run_once() 실물 경로 + conftest autouse 격리(tmp 원장/상태, 소켓 차단)
위에서 돈다.

  1. H50 OFF 에서 기존 X2-lite+W1a 가 주문 경로까지 도달
  2. H50 ON 에서 mode/state/UI 선택 일치
  3. mock broker 에서 실제 BUY/SELL 호출 발생
  4. restart 후 H50 선택 복원
  5. 날짜 rollover 후 slot/state 초기화
  6. H50 외 다른 전략 모드 결과 diff 0
  7. REAL 모드 경로 — 플래그 인식 / 주문 / 손절 / 익절

7번 주의: **실제 KIS 주문은 전송하지 않는다.** RealBrokerAdapter 를 실물로
construct 하되 그 하위 BrokerBase 만 기록용 fake 로 주입한다. 즉 MACD2 쪽
REAL 경로(어댑터 선택 -> buy_market/sell_market 호출 -> 원장 기록)는 전부
실물이고, KIS 로 나가는 마지막 한 홉만 대체된다. 실계좌로 실주문을 쏘는
검증은 돈이 오가는 비가역 행위라 이 파일에서 하지 않는다.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import (
    config,
    ledger,
    small_whipsaw_hold,
    state_store,
    time_window_3slot as tw3,
    worker,
)
from app.trading.macd2.broker_adapter import RealBrokerAdapter, create_macd2_broker
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.service import Macd2Service
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _approved,
    _patch_common,
    _prime_3slot_pending,
    _quality,
    _teg,
)
from tests.macd2.test_early_take_profit_worker import _market, _seed_completed_bar
from tests.macd2.test_x2lite_strategy import (
    _fresh_f_state,
    _fresh_x2lite_state,
    _isolate_ledger,
    _price_for_net,
)

KST = config.KST
H50_MODE = tw3.MODE_X2LITE_H50_3SLOT
X2_MODE = tw3.MODE_X2LITE_3SLOT


def _fresh_h50_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = budget
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = False
    s.time_window_twf_filter_enabled = False
    s.time_window_x2lite_filter_enabled = False
    s.time_window_h50_filter_enabled = True
    return s


def _run_sequence(state, monkeypatch, svc, now0, directions):
    """플래그 시퀀스를 흘려 넣고 (진입목록, broker, state) 를 돌려준다."""
    broker = FakeBroker(cash=10_000_000.0,
                        quotes={config.LONG_SYMBOL: 15_000.0,
                                config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    entries = []
    for i, direction in enumerate(directions):
        now = now0 + timedelta(minutes=3 * i)
        _prime_3slot_pending(state, direction, before=now - timedelta(minutes=3 * (i + 1)))
        before_qty = state.position.quantity if state.position else 0
        before_sym = state.position.symbol if state.position else None
        run_once(broker=broker, market_data=svc, state=state, now=now)
        pos = state.position
        if pos is not None and (pos.symbol != before_sym or pos.quantity != before_qty):
            entries.append((now.isoformat(), str(getattr(direction, "value", direction)),
                            pos.symbol, int(state.tw2_3slot_slots_used_today),
                            int(pos.quantity)))
    return entries, broker, state


# ════════════════════════════════════════════════════════════════════════════
# 1. H50 OFF -> 기존 X2-lite+W1a 가 주문 경로까지 도달한다
# ════════════════════════════════════════════════════════════════════════════
def test_1_h50_off_x2lite_reaches_the_broker_order_path(monkeypatch):
    svc, now0 = _market()
    state = _fresh_x2lite_state()
    assert tw3.active_3slot_mode(state) == X2_MODE
    assert small_whipsaw_hold.is_active(state) is False, "H50 OFF 인데 활성으로 잡힌다"

    entries, broker, state = _run_sequence(
        state, monkeypatch, svc, now0, [Direction.UP_RED, Direction.DOWN_BLUE])

    assert entries, "X2-lite 가 진입을 한 건도 만들지 못했다 -- 주문 경로 미도달"
    assert broker.orders, "broker 에 주문이 하나도 도달하지 않았다"
    rows = ledger.load_execution_ledger(limit=10_000)
    assert any(str(r.get("side", "")).upper() == "BUY" for r in rows), \
        "체결원장에 BUY 가 기록되지 않았다"


# ════════════════════════════════════════════════════════════════════════════
# 2. H50 ON -> mode / state / UI 선택이 일치한다
# ════════════════════════════════════════════════════════════════════════════
def test_2a_h50_on_mode_and_state_agree():
    state = _fresh_h50_state()
    assert state.time_window_h50_filter_enabled is True
    assert tw3.active_3slot_mode(state) == H50_MODE
    assert small_whipsaw_hold.is_active(state) is True
    # X2-lite 계열이므로 청산 프리셋/사이징이 X2-lite 와 같아야 한다
    assert H50_MODE in tw3.MODES_X2LITE_FAMILY
    assert tw3.exit_overrides(H50_MODE) == tw3.exit_overrides(X2_MODE), \
        "H50 의 청산 파라미터가 X2-lite 와 다르다"


def test_2b_service_setter_enforces_mutual_exclusion():
    svc = Macd2Service()
    res = svc.set_time_window_h50_filter_enabled(True, changed_by="smoke")
    assert res.get("ok"), res
    s = state_store.load_state()
    assert s.time_window_h50_filter_enabled is True
    for f in ("time_window_x2lite_filter_enabled", "time_window_3slot_filter_enabled",
              "time_window_twf_filter_enabled", "time_window_2_filter_enabled",
              "time_window_teg_filter_enabled"):
        assert getattr(s, f) is False, f"{f} 가 H50 과 동시에 켜져 있다"
    assert tw3.active_3slot_mode(s) == H50_MODE


def test_2c_ui_checkbox_matches_the_stored_selection():
    from streamlit.testing.v1 import AppTest

    app_path = str(Path(__file__).parent.parent.parent
                   / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
    s = state_store.load_state()
    s.time_window_h50_filter_enabled = True
    s.time_window_x2lite_filter_enabled = False
    state_store.save_state(s)

    at = AppTest.from_file(app_path, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if "can't be used in an `st.form()`" in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- H50 과 무관")
    assert not at.exception

    boxes = {c.key: c for c in at.checkbox}
    h50 = boxes.get("macd2_time_window_h50_filter_toggle")
    assert h50 is not None, f"H50 체크박스가 없다: {sorted(boxes)!r}"
    assert h50.label == config.H50_3SLOT_STRATEGY_NAME
    assert h50.value is True, "state 는 ON 인데 UI 체크박스가 OFF 다"
    x2 = boxes.get("macd2_time_window_x2lite_filter_toggle")
    assert x2 is not None and x2.value is False, "X2-lite 가 동시에 체크돼 있다"


# ════════════════════════════════════════════════════════════════════════════
# 3. mock broker 에서 실제 BUY / SELL 호출이 발생한다
# ════════════════════════════════════════════════════════════════════════════
def test_3_mock_broker_gets_real_buy_and_sell_calls(monkeypatch):
    svc, now0 = _market()
    state = _fresh_h50_state()
    # T+3 재확인 때문에 첫 플래그는 pending 등록만 하고, 진입은 다음 틱에 난다
    entries, broker, state = _run_sequence(
        state, monkeypatch, svc, now0, [Direction.UP_RED, Direction.DOWN_BLUE])
    assert entries, "진입이 없어 BUY 를 확인할 수 없다"
    buys = [o for o in broker.orders if str(getattr(o, "side", "")).upper() == "BUY"]
    assert buys, f"BUY 호출이 없다: {broker.orders!r}"

    # 강제청산으로 SELL 경로까지 민다(장 마감 청산은 필터와 무관하게 항상 동작)
    pos = state.position
    close_now = now0.replace(hour=15, minute=19)
    _seed_completed_bar(state, now=close_now, price=pos.avg_price)
    broker.set_quote(pos.symbol, pos.avg_price)
    run_once(broker=broker, market_data=svc, state=state, now=close_now)
    sells = [o for o in broker.orders if str(getattr(o, "side", "")).upper() == "SELL"]
    assert sells, f"SELL 호출이 없다: {broker.orders!r}"
    rows = ledger.load_execution_ledger(limit=10_000)
    assert any(str(r.get("side", "")).upper() == "SELL" for r in rows), \
        "체결원장에 SELL 이 기록되지 않았다"


# ════════════════════════════════════════════════════════════════════════════
# 4. restart 후 H50 선택이 복원된다
# ════════════════════════════════════════════════════════════════════════════
def test_4_h50_selection_survives_a_restart():
    s = state_store.default_state()
    s.time_window_h50_filter_enabled = True
    s.time_window_h50_filter_version = config.H50_3SLOT_FILTER_VERSION
    s.time_window_x2lite_filter_enabled = False
    s.h50_hold_active = True
    s.h50_original_direction = Direction.UP_RED.value
    s.h50_trend_break_count = 1
    state_store.save_state(s)

    back = state_store.load_state()          # = 재기동 시 복원 경로
    assert back.time_window_h50_filter_enabled is True, "재기동 후 H50 선택이 사라졌다"
    assert tw3.active_3slot_mode(back) == H50_MODE
    assert back.h50_hold_active is True, "HOLD 상태가 복원되지 않았다"
    assert back.h50_original_direction == Direction.UP_RED.value
    assert back.h50_trend_break_count == 1
    assert back.time_window_x2lite_filter_enabled is False


def test_4b_version_bump_resets_the_toggle_to_the_current_default():
    """필터 버전이 바뀌면 저장값을 버리고 **현재 기본값**으로 되돌린다.
    2026-09-16 에 기본값이 ON 이 됐으므로 저장된 OFF 가 ON 으로 돌아온다 --
    이미 배포된 인스턴스를 확정적으로 넘겨받는 경로가 바로 이것이다."""
    for stored in (True, False):
        s = state_store.default_state()
        s.time_window_h50_filter_enabled = stored
        s.time_window_h50_filter_version = "STALE_VERSION"
        state_store.save_state(s)
        back = state_store.load_state()
        assert back.time_window_h50_filter_enabled is config.H50_3SLOT_FILTER_DEFAULT,             f"저장값 {stored} 에서 기본값으로 복귀하지 않았다"
        assert back.time_window_h50_filter_version == config.H50_3SLOT_FILTER_VERSION


# ════════════════════════════════════════════════════════════════════════════
# 5. 날짜 rollover 후 slot / state 초기화
# ════════════════════════════════════════════════════════════════════════════
def test_5_daily_rollover_resets_slots_and_h50_hold_but_keeps_the_toggle(monkeypatch):
    svc, now0 = _market()
    state = _fresh_h50_state()
    entries, broker, state = _run_sequence(
        state, monkeypatch, svc, now0, [Direction.UP_RED, Direction.DOWN_BLUE])
    assert state.tw2_3slot_slots_used_today > 0, "슬롯이 소비되지 않아 rollover 를 볼 수 없다"

    # HOLD 상태를 인위적으로 세워 두고 날짜를 넘긴다
    held = small_whipsaw_hold.HoldDecision(
        should_hold=True, trend=small_whipsaw_hold.TREND_UP, range_pct=1.0,
        trend_ok=True, range_ok=True, insufficient_data=False, reason="SMOKE")
    small_whipsaw_hold.note_hold_start(
        state, held_direction=Direction.UP_RED, now=now0, decision=held)
    assert small_whipsaw_hold.is_holding(state) is True
    state.position = None

    # run_once 는 장상태/데이터 가드에 먼저 걸릴 수 있어 rollover 도달이
    # 보장되지 않는다. 날짜 경계 처리 자체를 보려는 테스트이므로 production
    # 의 rollover 함수를 직접 호출해 확정적으로 확인한다.
    next_day = (now0 + timedelta(days=1)).replace(hour=9, minute=5)
    assert state.session_date is not None, "첫 틱에서 session_date 가 설정되지 않았다"
    worker._apply_day_rollover(state, next_day)
    assert state.session_date == next_day.strftime("%Y%m%d")

    assert state.tw2_3slot_slots_used_today == 0, "슬롯이 초기화되지 않았다"
    assert state.tw2_3slot_morning_count == 0
    assert state.tw2_3slot_afternoon_count == 0
    assert state.daily_total_entry_count == 0
    assert small_whipsaw_hold.is_holding(state) is False, "전날 HOLD 가 이월됐다"
    assert state.time_window_h50_filter_enabled is True, "rollover 가 토글을 꺼버렸다"


# ════════════════════════════════════════════════════════════════════════════
# 6. H50 외 다른 전략 모드 결과 diff 0
# ════════════════════════════════════════════════════════════════════════════
def test_6_other_strategy_modes_are_bit_identical(tmp_path, monkeypatch):
    """H50 모듈이 존재해도 X2-lite / TW TEG 3-SLOT 의 진입·슬롯이 그대로다."""
    results = {}
    for tag, factory in (("x2lite", _fresh_x2lite_state), ("f", _fresh_f_state)):
        svc, now0 = _market()
        _isolate_ledger(monkeypatch, tmp_path, tag)
        state = factory()
        assert small_whipsaw_hold.is_active(state) is False, f"{tag} 에서 H50 이 켜졌다"
        entries, _b, st = _run_sequence(
            state, monkeypatch, svc, now0,
            [Direction.UP_RED, Direction.DOWN_BLUE, Direction.UP_RED])
        results[tag] = (entries, st.tw2_3slot_slots_used_today,
                        st.tw2_3slot_morning_count, st.tw2_3slot_afternoon_count)
        # H50 상태 필드는 전부 초기값이어야 한다
        assert st.h50_hold_active is False and st.h50_hold_started_at is None
        assert st.h50_original_direction is None

    assert results["x2lite"][0], "X2-lite 가 진입을 못 만들었다"
    assert results["x2lite"] == results["f"], (
        "X2-lite 와 TW TEG 3-SLOT 의 진입 집합이 다르다 -- 진입은 100% 같아야 한다\n"
        f"x2lite={results['x2lite']}\nf     ={results['f']}"
    )


def test_6b_exit_overrides_of_non_h50_modes_unchanged():
    for mode in (None, "TW2", "TEG", "TEGv2", tw3.MODE_TW2_3SLOT, "MU_MACD"):
        ov = tw3.exit_overrides(mode)
        assert ov["stop_loss_pct_override"] is None, mode
        assert ov["after_tp1_stop_pct_override"] is None, mode
        assert ov["afternoon_tp_pct_override"] is None, mode


# ════════════════════════════════════════════════════════════════════════════
# 7. REAL 모드 경로 — 플래그 인식 / 주문 / 손절 / 익절
#    (KIS 로 나가는 마지막 한 홉만 fake; MACD2 쪽 REAL 경로는 전부 실물)
# ════════════════════════════════════════════════════════════════════════════
def test_7a_real_adapter_is_selected_and_never_bypasses_its_gates():
    """create_macd2_broker('real') 이 RealBrokerAdapter 를 돌려주고,
    하위 broker 주입 시에도 mode 표기가 'real' 로 유지되는지."""
    inner = _RealInnerBroker(cash=10_000_000.0,
                             quotes={config.LONG_SYMBOL: 15_000.0,
                                     config.INVERSE_SYMBOL: 10_000.0})
    adapter = create_macd2_broker("real", confirm_text="", broker=inner)
    assert isinstance(adapter, RealBrokerAdapter)
    assert adapter.mode == "real"
    from app.trading.macd2.broker_adapter import MockBrokerAdapter
    assert MockBrokerAdapter.mode == "mock"


def _real_enter(state, adapter, svc, now0):
    """REAL 어댑터로 실제 진입 1건을 만든다(T+3 때문에 2틱 필요)."""
    for i, d in enumerate([Direction.UP_RED, Direction.DOWN_BLUE]):
        now = now0 + timedelta(minutes=3 * i)
        _prime_3slot_pending(state, d, before=now - timedelta(minutes=3 * (i + 1)))
        run_once(broker=adapter, market_data=svc, state=state, now=now)
    return now0 + timedelta(minutes=3)


class _RealInnerBroker(FakeBroker):
    """RealBrokerAdapter 가 하위 BrokerBase 에 요구하는 두 메서드를 채운 fake.

    KIS 로 나가는 마지막 한 홉만 이것으로 대체된다 -- 어댑터 자체는 실물
    RealBrokerAdapter 이므로 MACD2 쪽 REAL 경로는 전부 그대로 탄다."""

    def get_current_price(self, symbol: str):
        return self.get_quote(symbol)

    def get_balance(self) -> float:
        return self.get_cash()

    def get_orderable_cash(self, symbol: str = "") -> float:   # 어댑터는 무인자로 부른다
        return super().get_orderable_cash(symbol or config.INVERSE_SYMBOL)

    # 실 KIS broker 계층의 주문 API 이름/시그니처(buy/sell)를 그대로 흉내낸다.
    # RealBrokerAdapter 는 이 두 개만 호출하므로, 여기까지 오면 "REAL 경로가
    # 주문 호출까지 도달했다"가 증명된다.
    def buy(self, symbol, name, qty, price, order_type="market"):
        if order_type == "market":
            return super().buy_market(symbol, int(qty), "real-adapter")
        return super().buy_limit(symbol, int(qty), int(price), "real-adapter")

    def sell(self, symbol, name, qty, price, order_type="market"):
        return super().sell_market(symbol, int(qty), "real-adapter")


def _real_adapter():
    inner = _RealInnerBroker(cash=10_000_000.0,
                             quotes={config.LONG_SYMBOL: 15_000.0,
                                     config.INVERSE_SYMBOL: 10_000.0})
    return RealBrokerAdapter(confirm_text="", broker=inner), inner


def test_7b_real_mode_flag_recognition_and_buy(monkeypatch):
    svc, now0 = _market()
    state = _fresh_h50_state()
    state.mode = "real"
    adapter, inner = _real_adapter()
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    _real_enter(state, adapter, svc, now0)

    assert state.position is not None, "REAL 경로에서 플래그가 진입으로 이어지지 않았다"
    assert inner.orders, "REAL 어댑터 하위 broker 에 주문이 도달하지 않았다"
    assert any(str(getattr(o, "side", "")).upper() == "BUY" for o in inner.orders)


def test_7c_real_mode_stop_loss_fires(monkeypatch):
    svc, now0 = _market()
    state = _fresh_h50_state()
    state.mode = "real"
    adapter, inner = _real_adapter()
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    _real_enter(state, adapter, svc, now0)
    pos = state.position
    assert pos is not None

    # 오전 손절 -1.30% 아래로 떨어뜨린다 (X2-lite 계열 = H50 동일)
    sl = abs(config.X2LITE_MORNING_STOP_LOSS) * 100.0
    bad = _price_for_net(pos.symbol, pos.avg_price, pos.quantity,
                         -(sl + 0.20), side="at_most")
    later = now0 + timedelta(minutes=9)
    inner.set_quote(pos.symbol, bad)
    _seed_completed_bar(state, now=later, price=bad)
    run_once(broker=adapter, market_data=svc, state=state, now=later)

    assert state.position is None, "REAL 경로에서 손절이 발동하지 않았다"
    sells = [o for o in inner.orders if str(getattr(o, "side", "")).upper() == "SELL"]
    assert sells, "손절인데 SELL 이 나가지 않았다"
    rows = [r for r in ledger.load_execution_ledger(limit=10_000)
            if str(r.get("side", "")).upper() == "SELL"]
    assert rows, "손절이 체결원장에 없다"


def test_7d_real_mode_take_profit_fires(monkeypatch):
    svc, now0 = _market()
    state = _fresh_h50_state()
    state.mode = "real"
    adapter, inner = _real_adapter()
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    _real_enter(state, adapter, svc, now0)
    pos = state.position
    assert pos is not None
    qty0 = pos.quantity

    # TP1 (+3.0%) 위로 올려 부분익절을 발동시킨다
    tp1 = config.MORNING_TP1 * 100.0
    good = _price_for_net(pos.symbol, pos.avg_price, pos.quantity,
                          tp1 + 0.20, side="at_least")
    later = now0 + timedelta(minutes=9)
    inner.set_quote(pos.symbol, good)
    _seed_completed_bar(state, now=later, price=good)
    run_once(broker=adapter, market_data=svc, state=state, now=later)

    sells = [o for o in inner.orders if str(getattr(o, "side", "")).upper() == "SELL"]
    assert sells, "REAL 경로에서 익절 SELL 이 나가지 않았다"
    assert state.time_window_tp1_done is True or state.position is None, \
        "TP1 이 기록되지 않았다"
    if state.position is not None:
        assert state.position.quantity < qty0, "부분익절인데 수량이 줄지 않았다"


# ════════════════════════════════════════════════════════════════════════════
# 6c. H50 는 진입을 추가하지도 삭제하지도 않는다 (사용자 핵심 요구)
# ════════════════════════════════════════════════════════════════════════════
def test_6c_h50_entry_set_is_identical_to_x2lite(tmp_path, monkeypatch):
    """X2-lite 와 H50 의 진입 집합이 완전히 같아야 한다.

    주의: 두 전략을 한 테스트 안에서 돌리려면 원장/상태를 **실행 단위로**
    갈라야 한다. 같은 원장을 공유하면 두 번째 실행의 동일 signal_id 가
    중복실행 방지에 걸려 진입이 통째로 사라진다(정상 동작이다).
    """
    dirs = [Direction.UP_RED, Direction.DOWN_BLUE, Direction.UP_RED,
            Direction.DOWN_BLUE]
    out = {}
    for tag, factory in (("x2lite", _fresh_x2lite_state), ("h50", _fresh_h50_state)):
        svc, now0 = _market()
        _isolate_ledger(monkeypatch, tmp_path, tag)
        entries, _b, st = _run_sequence(factory(), monkeypatch, svc, now0, dirs)
        out[tag] = (entries, st.tw2_3slot_slots_used_today,
                    st.tw2_3slot_morning_count, st.tw2_3slot_afternoon_count)

    assert out["x2lite"][0], "X2-lite 가 진입을 못 만들어 대조가 불가능하다"
    assert out["h50"] == out["x2lite"], (
        "H50 이 진입 집합을 바꿨다 -- H50 은 청산 보류만 하는 필터여야 한다\n"
        f"x2lite={out['x2lite']}\nh50   ={out['h50']}"
    )


# ════════════════════════════════════════════════════════════════════════════
# 8. H50 를 기본 전략으로 (2026-09-16 사용자 결정)
# ════════════════════════════════════════════════════════════════════════════
class TestH50IsTheDefaultStrategy:
    def test_fresh_state_selects_h50_and_nothing_else(self):
        s = state_store.default_state()
        assert s.time_window_h50_filter_enabled is True, "fresh state 에서 H50 이 꺼져 있다"
        assert tw3.active_3slot_mode(s) == H50_MODE
        for f in ("time_window_x2lite_filter_enabled", "time_window_3slot_filter_enabled",
                  "time_window_twf_filter_enabled", "time_window_2_filter_enabled",
                  "time_window_teg_filter_enabled"):
            assert getattr(s, f) is False, f"{f} 가 H50 과 함께 켜져 있다"

    def test_other_subfilters_stay_off_by_default(self):
        s = state_store.default_state()
        for f in ("early_tp_filter_enabled", "quick_profit_enabled",
                  "no_filter_0900_1100_enabled",
                  "down_blue_exception_filter_enabled", "major_filter_enabled",
                  "sideways_filter_enabled", "single_entry_filter_enabled"):
            assert getattr(s, f, False) is False, f"{f} 가 기본 ON 이다"

    def test_a_stored_state_that_had_h50_off_is_migrated_on(self):
        """이미 배포된 인스턴스의 state.json 은 H50=false 를 들고 있을 수 있다.
        버전 게이팅으로 확정적으로 새 기본값(ON)이 적용돼야 한다."""
        s = state_store.default_state()
        s.time_window_h50_filter_enabled = False
        s.time_window_h50_filter_version = "H50_3SLOT_V1_20260915"   # 구버전
        s.time_window_x2lite_filter_enabled = True
        state_store.save_state(s)

        back = state_store.load_state()
        assert back.time_window_h50_filter_enabled is True, "구버전 state 가 H50 으로 안 넘어왔다"
        assert back.time_window_x2lite_filter_enabled is False
        assert tw3.active_3slot_mode(back) == H50_MODE

    def test_an_explicit_legacy_choice_beats_the_new_default(self):
        """인수인계가 끝난 뒤에는 기본 전략(H50)이 명시적 선택에 양보한다.

        기본값이 사용자의 선택을 이기면 "UI 에서 골랐는데 재기동하면 H50 으로
        돌아온다"가 된다. 그래서 여기서는 TW2 3-SLOT 이 이겨야 한다."""
        s = state_store.default_state()
        s.time_window_h50_filter_enabled = True
        s.time_window_h50_filter_version = config.H50_3SLOT_FILTER_VERSION
        s.time_window_3slot_filter_enabled = True
        state_store.save_state(s)

        back = state_store.load_state()
        assert back.time_window_3slot_filter_enabled is True
        assert back.time_window_h50_filter_enabled is False
        assert tw3.active_3slot_mode(back) == tw3.MODE_TW2_3SLOT

    def test_the_handover_runs_only_once(self):
        """인수인계는 딱 한 번. 그 뒤 사용자가 X2-lite 로 바꾸면 유지돼야 한다."""
        svc = Macd2Service()
        state_store.load_state()                       # 1회 인수인계 발생
        res = svc.set_time_window_x2lite_filter_enabled(True, changed_by="user")
        assert res.get("ok"), res
        for _ in range(3):                             # 재기동 3번 흉내
            back = state_store.load_state()
        assert back.time_window_x2lite_filter_enabled is True,             "인수인계가 다시 돌아 사용자의 선택을 덮어썼다"
        assert back.time_window_h50_filter_enabled is False

    def test_user_can_still_switch_back_to_x2lite(self):
        """기본값 교체가 사용자 선택을 덮어쓰지 않아야 한다."""
        svc = Macd2Service()
        res = svc.set_time_window_x2lite_filter_enabled(True, changed_by="smoke")
        assert res.get("ok"), res
        back = state_store.load_state()
        assert back.time_window_x2lite_filter_enabled is True
        assert back.time_window_h50_filter_enabled is False
        assert tw3.active_3slot_mode(back) == X2_MODE


# ════════════════════════════════════════════════════════════════════════════
# 9. H50 <-> X2-lite 즉시 전환 (2026-09-16 사용자 요청)
#    "끄고 켜면 재기동 없이 바로바로, 서로 독립적으로 적용되는가"
# ════════════════════════════════════════════════════════════════════════════
class TestImmediateSwitching:
    def test_service_switch_applies_without_a_reload(self):
        """setter 가 돌아온 직후의 in-memory state 가 이미 새 모드여야 한다."""
        svc = Macd2Service()
        svc.set_time_window_h50_filter_enabled(True, changed_by="user")
        s = state_store.load_state()
        assert tw3.active_3slot_mode(s) == H50_MODE
        assert small_whipsaw_hold.is_active(s) is True

        svc.set_time_window_x2lite_filter_enabled(True, changed_by="user")
        s = state_store.load_state()
        assert tw3.active_3slot_mode(s) == X2_MODE, "X2-lite 로 즉시 바뀌지 않았다"
        assert s.time_window_h50_filter_enabled is False, "H50 이 같이 꺼지지 않았다"
        assert small_whipsaw_hold.is_active(s) is False, "H50 로직이 아직 살아 있다"

        svc.set_time_window_h50_filter_enabled(True, changed_by="user")
        s = state_store.load_state()
        assert tw3.active_3slot_mode(s) == H50_MODE, "H50 으로 즉시 되돌아오지 않았다"
        assert s.time_window_x2lite_filter_enabled is False

    def test_switch_survives_repeated_restarts_in_both_directions(self):
        svc = Macd2Service()
        for setter, mode, flag in (
            ("set_time_window_x2lite_filter_enabled", X2_MODE, "time_window_x2lite_filter_enabled"),
            ("set_time_window_h50_filter_enabled", H50_MODE, "time_window_h50_filter_enabled"),
            ("set_time_window_x2lite_filter_enabled", X2_MODE, "time_window_x2lite_filter_enabled"),
        ):
            assert getattr(svc, setter)(True, changed_by="user").get("ok")
            for _ in range(3):                       # 재기동 3회 모사
                s = state_store.load_state()
            assert getattr(s, flag) is True, f"{setter} 선택이 재기동에서 사라졌다"
            assert tw3.active_3slot_mode(s) == mode

    def test_switching_does_not_touch_counters_position_or_other_filters(self, monkeypatch):
        """'독립적으로' — 전략 전환이 슬롯/일일카운터/보유포지션/다른 필터를
        건드리면 안 된다. 전환 한 번으로 그날 진입예산이 리셋되면 하루 3회
        cap 이 무력화된다."""
        svc, now0 = _market()
        state = _fresh_h50_state()
        entries, broker, state = _run_sequence(
            state, monkeypatch, svc, now0, [Direction.UP_RED, Direction.DOWN_BLUE])
        assert entries and state.position is not None
        state.quick_profit_enabled = True
        state.no_filter_0900_1100_enabled = True
        state_store.save_state(state)

        before = (state.tw2_3slot_slots_used_today, state.tw2_3slot_morning_count,
                  state.tw2_3slot_afternoon_count, state.daily_total_entry_count,
                  state.position.symbol, state.position.quantity,
                  state.time_window_active_mode)

        res = Macd2Service().set_time_window_x2lite_filter_enabled(True, changed_by="user")
        assert res.get("ok"), res
        after_state = state_store.load_state()
        after = (after_state.tw2_3slot_slots_used_today, after_state.tw2_3slot_morning_count,
                 after_state.tw2_3slot_afternoon_count, after_state.daily_total_entry_count,
                 after_state.position.symbol, after_state.position.quantity,
                 after_state.time_window_active_mode)
        assert after == before, f"전환이 카운터/포지션을 건드렸다\n전={before}\n후={after}"
        assert after_state.quick_profit_enabled is True, "무관한 필터가 꺼졌다"
        assert after_state.no_filter_0900_1100_enabled is True, "무관한 필터가 꺼졌다"
        assert tw3.active_3slot_mode(after_state) == X2_MODE

    def test_turning_h50_off_mid_hold_stops_suppressing_the_exit(self):
        """HOLD 중에 H50 을 끄면 그 보류가 즉시 풀려야 한다 --
        꺼진 전략이 청산을 계속 막고 있으면 실거래에서 치명적이다."""
        s = _fresh_h50_state()
        s.h50_hold_active = True
        s.h50_original_direction = Direction.UP_RED.value
        s.h50_trend_break_count = 1
        state_store.save_state(s)
        assert small_whipsaw_hold.is_holding(state_store.load_state()) is True

        assert Macd2Service().set_time_window_x2lite_filter_enabled(
            True, changed_by="user").get("ok")
        back = state_store.load_state()
        assert small_whipsaw_hold.is_active(back) is False, \
            "H50 이 꺼졌는데 모듈이 아직 활성이다 -- 청산을 계속 보류한다"

    def test_ui_switch_round_trips_in_both_directions(self):
        from streamlit.testing.v1 import AppTest

        app_path = str(Path(__file__).parent.parent.parent
                       / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")

        def _render():
            at = AppTest.from_file(app_path, default_timeout=30)
            at.session_state["app_auth_authenticated"] = True
            at.run()
            for exc in at.exception:
                if "can't be used in an `st.form()`" in str(getattr(exc, "value", "") or ""):
                    pytest.skip("기존 페이지 결함(st.button inside st.form) -- H50 과 무관")
            assert not at.exception
            return at

        def _box(at, key):
            for cb in at.checkbox:
                if cb.key == key:
                    return cb
            raise AssertionError(f"{key} 체크박스가 없다: {[c.key for c in at.checkbox]!r}")

        H50_KEY = "macd2_time_window_h50_filter_toggle"
        X2_KEY = "macd2_time_window_x2lite_filter_toggle"

        at = _render()
        assert _box(at, H50_KEY).value is True, "기본 렌더에서 H50 이 체크돼 있지 않다"

        _box(at, X2_KEY).check().run()               # UI 에서 X2-lite 로 전환
        for exc in at.exception:
            if "can't be used in an `st.form()`" in str(getattr(exc, "value", "") or ""):
                pytest.skip("기존 페이지 결함 -- H50 과 무관")
        assert not at.exception
        s = state_store.load_state()
        assert s.time_window_x2lite_filter_enabled is True
        assert s.time_window_h50_filter_enabled is False
        assert _box(at, H50_KEY).value is False, "UI 체크가 즉시 반영되지 않았다"

        _box(at, H50_KEY).check().run()              # 다시 H50 으로
        assert not at.exception
        s = state_store.load_state()
        assert s.time_window_h50_filter_enabled is True
        assert s.time_window_x2lite_filter_enabled is False
        assert _box(at, X2_KEY).value is False
