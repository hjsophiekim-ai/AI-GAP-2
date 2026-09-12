"""X2-lite + W1a 포지션 사이징 — production 적용 검증 (2026-09-12).

W1a 는 **주문수량만** 바꾼다. 진입/청산 판정은 한 줄도 건드리지 않는다.
이 파일은 세 축을 증명한다:

  A. **기존 전략 회귀 0** — X2-lite 가 꺼져 있으면 배수가 항상 1.0 이고
     budget 이 그대로 전달된다(F / TW TEG 3-SLOT / TW2 3-SLOT / 무필터).
  B. **사이징 정확성** — 100 / 80 / 120 / 96%, clip 25~150%, 일일 300% greedy cap,
     "그날 첫 거래 STOP_LOSS" 정의, 날짜 rollover, 재시작 복원.
  C. **청산 불변** — TP1 20% / TP2 5% / trailing 2.8% / SL −1.3% / ETP 1.5→1.0 /
     반대신호 / switch / 강제청산이 사이징 크기와 무관하게 동일하게 작동한다.

시나리오는 전부 **실제 worker.run_once() + FakeBroker + 격리 tmp 원장**
(conftest autouse)으로 end-to-end 검증한다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import (
    config,
    ledger,
    order_executor,
    position_sizing as PS,
    state_store,
    time_window_3slot as tw3,
    worker,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _PRIOR_DAY, _SESSION_START_NOW, _approved, _patch_common, _prime_3slot_pending,
    _quality, _rejected, _teg,
)
from tests.macd2.test_early_take_profit_worker import _market, _seed_completed_bar
from tests.macd2.test_x2lite_strategy import (
    _fresh_f_state, _fresh_x2lite_state, _price_for_net, _sell_rows,
)

MODE = tw3.MODE_X2LITE_3SLOT
CHOP_M = config.X2LITE_SIZING_CHOP_MULT      # 0.80
POST_M = config.X2LITE_SIZING_POST_STOP_MULT  # 1.20
CAP = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP  # 3.00


def _x2l() -> RuntimeState:
    return _fresh_x2lite_state()


# ════════════════════════════════════════════════════════════════════════════
# A. 기존 전략 회귀 — X2-lite 가 아니면 배수 1.0, 상태 미변경
# ════════════════════════════════════════════════════════════════════════════
def test_sizing_is_inactive_for_every_other_mode():
    for factory, lbl in ((_fresh_f_state, "TW TEG 3-SLOT"),):
        s = factory()
        assert PS.is_active(s) is False, lbl
        assert PS.evaluate(s, entry_chop=True) is PS.NEUTRAL
        assert PS.evaluate(s, entry_chop=False).applied == pytest.approx(1.0)
    for flag in ("time_window_3slot_filter_enabled", "time_window_2_filter_enabled",
                 "time_window_teg_filter_enabled"):
        s = state_store.default_state()
        for f in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
                  "time_window_x2lite_filter_enabled", "time_window_2_filter_enabled",
                  "time_window_teg_filter_enabled"):
            setattr(s, f, False)
        setattr(s, flag, True)
        assert PS.is_active(s) is False, flag
    s = state_store.default_state()   # 전부 끄면 3-SLOT 계열 모드가 없다
    for f in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled"):
        setattr(s, f, False)
    assert tw3.active_3slot_mode(s) is None
    assert PS.is_active(s) is False


def test_note_entry_and_exit_are_noop_when_inactive():
    s = _fresh_f_state()
    PS.note_entry(s, PS.NEUTRAL)
    PS.note_full_exit(s, config.EXIT_TW_STOP_LOSS)
    assert s.x2lite_exposure_used_today == pytest.approx(0.0)
    assert s.x2lite_entry_seq_today == 0
    assert s.x2lite_first_trade_stop_loss is False


def test_f_mode_budget_is_passed_through_unchanged(monkeypatch):
    """F 선택 시 execute_signal 이 받는 budget 이 state.budget 과 정확히 같다."""
    seen = {}
    real = order_executor.execute_signal

    def spy(*a, **kw):
        seen["budget"] = kw.get("budget")
        return real(*a, **kw)

    svc, now0 = _market()
    state = _fresh_f_state(budget=7_000_000.0)
    broker = FakeBroker(cash=10_000_000.0,
                        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    monkeypatch.setattr(worker.order_executor, "execute_signal", spy)
    _prime_3slot_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert seen.get("budget") == pytest.approx(7_000_000.0)


# ════════════════════════════════════════════════════════════════════════════
# B-1. 배수 계산 — 100 / 80 / 120 / 96
# ════════════════════════════════════════════════════════════════════════════
def test_multiplier_base_100():
    s = _x2l()
    d = PS.evaluate(s, entry_chop=False)
    assert d.active is True
    assert d.applied == pytest.approx(1.00)
    assert d.reason == "BASE"


def test_multiplier_chop_80():
    s = _x2l()
    d = PS.evaluate(s, entry_chop=True)
    assert d.applied == pytest.approx(0.80)
    assert d.reason == "CHOP"


def test_multiplier_post_stop_120():
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    d = PS.evaluate(s, entry_chop=False)
    assert d.applied == pytest.approx(1.20)
    assert d.reason == "POST_STOP"


def test_multiplier_chop_and_post_stop_96():
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    d = PS.evaluate(s, entry_chop=True)
    assert d.raw == pytest.approx(0.96)
    assert d.applied == pytest.approx(0.96)
    assert d.reason == "CHOP+POST_STOP"


def test_multiplier_is_clipped_to_25_150(monkeypatch):
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    monkeypatch.setattr(config, "X2LITE_SIZING_POST_STOP_MULT", 3.0)
    assert PS.evaluate(s, entry_chop=False).clipped == pytest.approx(1.50)
    monkeypatch.setattr(config, "X2LITE_SIZING_POST_STOP_MULT", 1.0)
    monkeypatch.setattr(config, "X2LITE_SIZING_CHOP_MULT", 0.01)
    assert PS.evaluate(s, entry_chop=True).clipped == pytest.approx(0.25)


# ════════════════════════════════════════════════════════════════════════════
# B-2. 일일 exposure 300% greedy cap
# ════════════════════════════════════════════════════════════════════════════
def test_daily_cap_greedy_truncates_the_last_slot():
    """slot1 100% + slot2 120% = 220%, slot3 가 120% 를 요청하면 80% 만 허용."""
    s = _x2l()
    d1 = PS.evaluate(s, entry_chop=False)
    assert d1.applied == pytest.approx(1.00)
    PS.note_entry(s, d1)
    s.x2lite_first_trade_stop_loss = True
    d2 = PS.evaluate(s, entry_chop=False)
    assert d2.applied == pytest.approx(1.20) and d2.capped is False
    PS.note_entry(s, d2)
    assert s.x2lite_exposure_used_today == pytest.approx(2.20)

    d3 = PS.evaluate(s, entry_chop=False)
    assert d3.clipped == pytest.approx(1.20)
    assert d3.applied == pytest.approx(0.80), "300% - 220% = 80% 만 허용"
    assert d3.capped is True
    assert "CAPPED" in d3.reason
    PS.note_entry(s, d3)
    assert s.x2lite_exposure_used_today == pytest.approx(CAP)


def test_daily_cap_zero_room_gives_zero():
    s = _x2l()
    s.x2lite_exposure_used_today = CAP
    d = PS.evaluate(s, entry_chop=False)
    assert d.applied == pytest.approx(0.0) and d.capped is True


def test_exposure_is_entry_time_cumulative_and_never_returned_by_exits():
    """부분익절/청산이 누적 exposure 를 되돌리지 않는다(연구 사양)."""
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    assert s.x2lite_exposure_used_today == pytest.approx(1.0)
    PS.note_full_exit(s, config.EXIT_TW_TP2_FULL)
    assert s.x2lite_exposure_used_today == pytest.approx(1.0), "청산이 되돌리면 안 된다"


# ════════════════════════════════════════════════════════════════════════════
# B-3. "그날 첫 거래 STOP_LOSS" 정의
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reason,expected", [
    (config.EXIT_TW_STOP_LOSS, True),                 # 유일하게 활성
    (config.EXIT_TW_AFTER_TP1_STOP, False),           # TP1 이후 잔량 stop
    (config.EXIT_TW_TRAILING_STOP, False),
    (config.EXIT_TW_TP2_FULL, False),
    (config.EXIT_TW_TP1_PARTIAL, False),
    (config.EXIT_TW_AFTERNOON_TP, False),
    (config.EXIT_TW_BREAKEVEN_STOP, False),
    (config.EXIT_TW_PROFIT_LOCK_STOP, False),
    (config.EXIT_OPPOSITE_SIGNAL, False),
    (config.EXIT_FORCED_LIQUIDATION, False),
    (config.EXIT_EARLY_TAKE_PROFIT, False),
    ("WHIPSAW_WATCH_DETERIORATION_EXIT", False),
    (None, False),
])
def test_first_trade_stop_loss_definition(reason, expected):
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))   # 첫 거래 진입
    PS.note_full_exit(s, reason)
    assert s.x2lite_first_trade_stop_loss is expected, reason


def test_only_the_first_trade_sets_the_flag():
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    PS.note_full_exit(s, config.EXIT_TW_TP2_FULL)        # 첫 거래는 익절
    assert s.x2lite_first_trade_stop_loss is False
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))   # 두 번째 진입
    PS.note_full_exit(s, config.EXIT_TW_STOP_LOSS)       # 두 번째가 손절이어도
    assert s.x2lite_first_trade_stop_loss is False, "첫 거래만 플래그를 세운다"


def test_flag_is_not_downgraded_once_set():
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    PS.note_full_exit(s, config.EXIT_TW_STOP_LOSS)
    assert s.x2lite_first_trade_stop_loss is True
    PS.note_full_exit(s, config.EXIT_TW_TP2_FULL)
    assert s.x2lite_first_trade_stop_loss is True


def test_day_rollover_resets_everything_but_the_toggle():
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    PS.note_full_exit(s, config.EXIT_TW_STOP_LOSS)
    assert (s.x2lite_exposure_used_today, s.x2lite_entry_seq_today,
            s.x2lite_first_trade_stop_loss) == (pytest.approx(1.0), 1, True)
    PS.reset_daily(s)
    assert s.x2lite_exposure_used_today == pytest.approx(0.0)
    assert s.x2lite_entry_seq_today == 0
    assert s.x2lite_first_trade_stop_loss is False
    assert s.time_window_x2lite_filter_enabled is True, "토글은 유지"


def test_state_roundtrip_restores_sizing_state():
    """재시작 복원 — 동일 후보의 배수가 재시작 전/후 같아야 한다."""
    s = _x2l()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    s.x2lite_first_trade_stop_loss = True
    PS.note_entry(s, PS.evaluate(s, entry_chop=False))
    before = PS.evaluate(s, entry_chop=True)
    state_store.save_state(s)

    back = state_store.load_state()
    assert back.x2lite_exposure_used_today == pytest.approx(s.x2lite_exposure_used_today)
    assert back.x2lite_entry_seq_today == s.x2lite_entry_seq_today
    assert back.x2lite_first_trade_stop_loss is True
    after = PS.evaluate(back, entry_chop=True)
    assert after.applied == pytest.approx(before.applied)
    assert after.reason == before.reason


def test_backward_compatible_with_a_state_json_without_the_new_keys(tmp_path):
    """기존 state.json (신규 키 없음) 을 읽어도 기본값으로 안전하게 복원된다."""
    import json, io as _io
    s = _x2l()
    state_store.save_state(s)
    raw = json.loads(_io.open(state_store.STATE_PATH, encoding="utf-8").read())
    for k in ("x2lite_exposure_used_today", "x2lite_entry_seq_today",
              "x2lite_first_trade_stop_loss", "x2lite_last_applied_sizing"):
        raw.pop(k, None)
    _io.open(state_store.STATE_PATH, "w", encoding="utf-8").write(
        json.dumps(raw, ensure_ascii=False))
    back = state_store.load_state()
    assert back.x2lite_exposure_used_today == pytest.approx(0.0)
    assert back.x2lite_entry_seq_today == 0
    assert back.x2lite_first_trade_stop_loss is False
    assert PS.evaluate(back, entry_chop=False).applied == pytest.approx(1.0)


# ════════════════════════════════════════════════════════════════════════════
# B-4. worker end-to-end — 실제 주문수량
# ════════════════════════════════════════════════════════════════════════════
def _spy_budget(monkeypatch, sink):
    real = order_executor.execute_signal

    def spy(*a, **kw):
        sink.append(kw.get("budget"))
        return real(*a, **kw)
    monkeypatch.setattr(worker.order_executor, "execute_signal", spy)


def _enter_once(monkeypatch, state, *, chop, budget=5_000_000.0, price=10_000.0):
    """플래그 하나를 흘려 X2-lite 진입 1건을 만들고 (budget, outcome qty) 반환."""
    svc, now0 = _market(inverse_price=price)
    state.budget = budget
    broker = FakeBroker(cash=50_000_000.0,
                        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: price})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **kw: _chop_decision(chop))
    seen: list = []
    _spy_budget(monkeypatch, seen)
    _prime_3slot_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    return seen, state, broker


def _chop_decision(is_chop: bool):
    from app.trading.macd2.early_take_profit import EntryChopDecision
    return EntryChopDecision(
        is_chop=bool(is_chop), score=(3 if is_chop else 0), required=3,
        conditions={}, metrics={}, insufficient_data=False,
    )


def test_e2e_base_entry_uses_full_budget(monkeypatch):
    seen, state, broker = _enter_once(monkeypatch, _x2l(), chop=False)
    assert seen and seen[0] == pytest.approx(5_000_000.0)
    assert state.position is not None
    assert state.x2lite_exposure_used_today == pytest.approx(1.00)
    assert state.x2lite_entry_seq_today == 1


def test_e2e_chop_entry_uses_80pct_budget(monkeypatch):
    seen, state, broker = _enter_once(monkeypatch, _x2l(), chop=True)
    assert seen and seen[0] == pytest.approx(5_000_000.0 * CHOP_M)
    assert state.position is not None
    assert state.x2lite_exposure_used_today == pytest.approx(CHOP_M)
    assert state.time_window_entry_chop is True, "사이징에 쓴 CHOP 이 포지션에도 저장"


def test_e2e_post_stop_entry_uses_120pct_budget(monkeypatch):
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    s.x2lite_entry_seq_today = 1
    s.x2lite_exposure_used_today = 1.0
    seen, state, broker = _enter_once(monkeypatch, s, chop=False)
    assert seen and seen[0] == pytest.approx(5_000_000.0 * POST_M)
    assert state.x2lite_exposure_used_today == pytest.approx(1.0 + POST_M)


def test_e2e_chop_and_post_stop_entry_uses_96pct_budget(monkeypatch):
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    s.x2lite_entry_seq_today = 1
    s.x2lite_exposure_used_today = 1.0
    seen, state, broker = _enter_once(monkeypatch, s, chop=True)
    assert seen and seen[0] == pytest.approx(5_000_000.0 * CHOP_M * POST_M)


def test_e2e_daily_cap_truncates_the_third_slot_budget(monkeypatch):
    """누적 220% 상태에서 120% 요청 -> 80% 만 나가야 한다."""
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    s.x2lite_entry_seq_today = 2
    s.x2lite_exposure_used_today = 2.20
    seen, state, broker = _enter_once(monkeypatch, s, chop=False)
    assert seen and seen[0] == pytest.approx(5_000_000.0 * 0.80)
    assert state.x2lite_exposure_used_today == pytest.approx(CAP)


def test_e2e_order_qty_never_exceeds_orderable_cash(monkeypatch):
    """120% 사이징이라도 주문가능금액을 넘는 주문은 나갈 수 없다."""
    s = _x2l()
    s.x2lite_first_trade_stop_loss = True
    s.x2lite_entry_seq_today = 1
    svc, now0 = _market(inverse_price=10_000.0)
    s.budget = 100_000_000.0            # 예산은 과하게
    broker = FakeBroker(cash=3_000_000.0,   # 실제 주문가능금액은 300만
                        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **kw: _chop_decision(False))
    _prime_3slot_pending(s, Direction.DOWN_BLUE, before=_PRIOR_DAY)
    run_once(broker=broker, market_data=svc, state=s, now=now0)
    assert s.position is not None
    notional = s.position.quantity * 10_000.0
    assert notional <= 3_000_000.0, f"주문가능금액 초과 ({notional})"


def test_compute_limit_buy_quantity_scales_with_budget():
    """배수는 budget 에만 곱해지고 수량 산출식 자체는 그대로다."""
    base = order_executor.compute_limit_buy_quantity(
        ui_budget=1_000_000.0, orderable_cash=50_000_000.0,
        order_price=10_000.0, limit_buyable_qty=10_000)
    sized = order_executor.compute_limit_buy_quantity(
        ui_budget=1_000_000.0 * 1.20, orderable_cash=50_000_000.0,
        order_price=10_000.0, limit_buyable_qty=10_000)
    assert base[2] == 99 and sized[2] == 119
    capped = order_executor.compute_limit_buy_quantity(
        ui_budget=1_000_000.0 * 1.20, orderable_cash=500_000.0,
        order_price=10_000.0, limit_buyable_qty=10_000)
    assert capped[2] == 49, "orderable_cash 가 상한"


# ════════════════════════════════════════════════════════════════════════════
# C. 청산 불변 — 사이징 크기와 무관
# ════════════════════════════════════════════════════════════════════════════
def _held(qty, *, entry=10_000.0, tp1_done=False, peak=0.0, entry_chop=False,
          bar_close=None, now=None, exposure=1.0, seq=1):
    s = _x2l()
    now = now or _SESSION_START_NOW
    s.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty,
                                  avg_price=entry, entry_at=now)
    s.time_window_position_active = True
    s.time_window_active_mode = MODE
    s.time_window_entry_session = "MORNING"
    s.time_window_tp1_done = tp1_done
    s.time_window_peak_net_return = peak
    s.early_tp_peak_net_return = peak
    s.time_window_entry_chop = entry_chop
    s.tw2_3slot_slots_used_today = seq
    s.x2lite_entry_seq_today = seq
    s.x2lite_exposure_used_today = exposure
    _seed_completed_bar(s, now=now, price=(entry if bar_close is None else bar_close))
    return s


def _broker(price, *, entry=10_000.0, qty=1000):
    b = FakeBroker(cash=50_000_000.0,
                   quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: price})
    b.buy_market(config.INVERSE_SYMBOL, qty, "seed")
    b._positions[config.INVERSE_SYMBOL].avg_price = entry
    return b


@pytest.mark.parametrize("qty,sold,left", [(1000, 200, 800), (960, 192, 768), (1200, 240, 960)])
def test_tp1_sells_exactly_20pct_at_any_sizing(monkeypatch, qty, sold, left):
    entry = 10_000.0
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 3.0)
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, now=now0)
    b = _broker(price, entry=entry, qty=qty)
    _patch_common(monkeypatch)
    r = run_once(broker=b, market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_TP1_PARTIAL) for a in r.actions), r.actions
    assert s.position.quantity == left
    rows = [x for x in _sell_rows() if x.get("exit_reason") == config.EXIT_TW_TP1_PARTIAL]
    assert len(rows) == 1
    assert int(rows[0]["position_before"]) == qty
    assert int(rows[0]["executed_qty"]) == sold
    assert int(rows[0]["position_after"]) == left


def test_tp2_full_exit_at_5pct(monkeypatch):
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 5.0)
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, tp1_done=True, peak=3.2, now=now0)
    b = _broker(price, entry=entry, qty=qty)
    _patch_common(monkeypatch)
    r = run_once(broker=b, market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in r.actions), r.actions
    assert s.position is None
    rows = [x for x in _sell_rows() if x.get("exit_reason") == config.EXIT_TW_TP2_FULL]
    assert len(rows) == 1 and int(rows[0]["position_after"]) == 0
    sells = len([o for o in b.orders if o.side.upper() == "SELL"])
    run_once(broker=b, market_data=svc, state=s, now=now0 + timedelta(minutes=3))
    assert len([o for o in b.orders if o.side.upper() == "SELL"]) == sells


@pytest.mark.parametrize("net,fires", [(2.81, False), (2.80, True)])
def test_trailing_28_boundary_unchanged(monkeypatch, net, fires):
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, net,
                           side=("at_least" if not fires else "at_most"))
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, tp1_done=True, peak=4.0, bar_close=price, now=now0)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(price, entry=entry, qty=qty), market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_TRAILING_STOP) for a in r.actions) is fires


@pytest.mark.parametrize("net,fires", [(-1.29, False), (-1.30, True)])
def test_morning_sl_130_boundary_unchanged(monkeypatch, net, fires):
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, net,
                           side=("at_least" if not fires else "at_most"))
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, bar_close=price, now=now0)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(price, entry=entry, qty=qty), market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in r.actions) is fires
    if fires:
        assert s.x2lite_first_trade_stop_loss is True, "첫 거래 손절 -> 이후 120%"
        assert PS.evaluate(s, entry_chop=False).applied == pytest.approx(POST_M)


def test_stop_loss_on_second_trade_does_not_arm_post_stop(monkeypatch):
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, -1.30, side="at_most")
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, bar_close=price, now=now0, seq=2, exposure=2.0)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(price, entry=entry, qty=qty), market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in r.actions)
    assert s.x2lite_first_trade_stop_loss is False, "두 번째 거래는 플래그를 세우지 않는다"


def test_after_tp1_stop_does_not_arm_post_stop(monkeypatch):
    """첫 거래가 TP1 이후 잔량 stop 으로 끝나면 120% 가 켜지면 안 된다."""
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 2.0, side="at_most")
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, tp1_done=True, peak=3.0, bar_close=price, now=now0)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(price, entry=entry, qty=qty), market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_TW_AFTER_TP1_STOP) for a in r.actions), r.actions
    assert s.x2lite_first_trade_stop_loss is False


@pytest.mark.parametrize("peak,bar_net,fires", [(1.49, 0.50, False), (1.50, 1.01, False),
                                                (1.50, 1.00, True)])
def test_etp_state_machine_unchanged(monkeypatch, peak, bar_net, fires):
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, bar_net,
                           side=("at_least" if not fires else "at_most"))
    svc, now0 = _market(inverse_price=price)
    s = _held(qty, entry=entry, peak=peak, entry_chop=True, bar_close=price, now=now0)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(price, entry=entry, qty=qty), market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in r.actions) is fires
    if fires:
        assert s.x2lite_first_trade_stop_loss is False, "ETP 는 STOP_LOSS 가 아니다"


def test_forced_liquidation_closes_all_and_keeps_exposure(monkeypatch):
    entry, qty = 10_000.0, 800
    svc, _ = _market(inverse_price=entry)
    now = _SESSION_START_NOW.replace(hour=15, minute=0, second=5, microsecond=0)
    s = _held(qty, entry=entry, now=now, exposure=2.4)
    slots = s.tw2_3slot_slots_used_today
    _patch_common(monkeypatch)
    r = run_once(broker=_broker(entry, entry=entry, qty=qty), market_data=svc, state=s, now=now)
    assert any(config.EXIT_FORCED_LIQUIDATION in a for a in r.actions), r.actions
    assert s.position is None
    assert s.tw2_3slot_slots_used_today == slots
    assert s.x2lite_exposure_used_today == pytest.approx(2.4), "청산이 exposure 를 되돌리지 않는다"
    assert s.x2lite_first_trade_stop_loss is False


def test_whipsaw_reversal_holds_and_does_not_touch_sizing(monkeypatch):
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    s = _held(qty, entry=entry, now=now0, exposure=1.0)
    s.tw2_3slot_morning_count = 1
    b = _broker(entry, entry=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_rejected(sorted(config.TW_WHIPSAW_REJECT_REASONS)[0]))
    _prime_3slot_pending(s, Direction.UP_RED, before=_PRIOR_DAY)
    orders = len(b.orders)
    r = run_once(broker=b, market_data=svc, state=s, now=now0)
    assert any(a.startswith("TW2_3SLOT_WHIPSAW_HOLD") for a in r.actions), r.actions
    assert len(b.orders) == orders
    assert s.x2lite_exposure_used_today == pytest.approx(1.0)
    assert s.x2lite_entry_seq_today == 1


def test_non_whipsaw_reversal_liquidates_without_arming_post_stop(monkeypatch):
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    s = _held(qty, entry=entry, now=now0)
    b = _broker(entry, entry=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE))
    _prime_3slot_pending(s, Direction.UP_RED, before=_PRIOR_DAY)
    r = run_once(broker=b, market_data=svc, state=s, now=now0)
    assert any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in r.actions), r.actions
    assert s.position is None
    assert s.x2lite_first_trade_stop_loss is False, "OPPOSITE_SIGNAL 은 STOP_LOSS 가 아니다"


def test_switch_entry_recomputes_sizing_and_consumes_a_slot(monkeypatch):
    """반대신호 SWITCH 의 신규 진입에도 그 시점 W1a 배수가 적용된다."""
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    s = _held(qty, entry=entry, now=now0, exposure=1.0)
    s.x2lite_first_trade_stop_loss = True          # 이후 진입은 120%
    s.budget = 5_000_000.0
    s.tw2_3slot_morning_count = 1
    b = _broker(entry, entry=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **kw: _chop_decision(False))
    seen: list = []
    _spy_budget(monkeypatch, seen)
    _prime_3slot_pending(s, Direction.UP_RED, before=_PRIOR_DAY)
    r = run_once(broker=b, market_data=svc, state=s, now=now0)
    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in r.actions), r.actions
    assert seen and seen[0] == pytest.approx(5_000_000.0 * POST_M)
    assert s.tw2_3slot_slots_used_today == 2
    assert s.x2lite_exposure_used_today == pytest.approx(1.0 + POST_M)
    assert s.time_window_active_mode == MODE


# ════════════════════════════════════════════════════════════════════════════
# D. 배포 준비 — 기존 state.json 에서 UI 토글 1회로 활성화되는가
# ════════════════════════════════════════════════════════════════════════════
def test_legacy_state_is_adopted_into_x2lite_without_any_manual_action():
    """Render 에 이미 있는 state.json(신규 키 없음, TW2 3-SLOT ON) 을 읽으면
    **사람이 아무것도 하지 않아도** X2-lite + W1a 가 켜진 상태로 복원된다."""
    import json, io as _io

    legacy = state_store.default_state()
    legacy.time_window_3slot_filter_enabled = True
    legacy.time_window_twf_filter_enabled = False
    legacy.time_window_x2lite_filter_enabled = False
    state_store.save_state(legacy)
    raw = json.loads(_io.open(state_store.STATE_PATH, encoding="utf-8").read())
    for k in ("time_window_x2lite_filter_enabled", "time_window_x2lite_filter_version",
              "x2lite_exposure_used_today", "x2lite_entry_seq_today",
              "x2lite_first_trade_stop_loss", "x2lite_last_applied_sizing"):
        raw.pop(k, None)
    _io.open(state_store.STATE_PATH, "w", encoding="utf-8").write(
        json.dumps(raw, ensure_ascii=False))

    s = state_store.load_state()          # 재배포 직후 첫 복원
    assert s.time_window_x2lite_filter_enabled is True
    assert s.time_window_3slot_filter_enabled is False
    assert s.time_window_twf_filter_enabled is False
    assert tw3.active_3slot_mode(s) == MODE
    assert PS.is_active(s) is True
    assert PS.evaluate(s, entry_chop=True).applied == pytest.approx(CHOP_M)
    assert s.x2lite_exposure_used_today == pytest.approx(0.0)
    assert s.x2lite_first_trade_stop_loss is False

    state_store.save_state(s)
    again = state_store.load_state()      # 재시작해도 유지
    assert again.time_window_x2lite_filter_enabled is True
    assert PS.is_active(again) is True


def test_worker_reads_x2lite_mode_and_exit_overrides_after_toggle():
    """토글 후 worker 가 실제로 X2-lite 청산 파라미터를 쓰는지."""
    from app.trading.macd2.service import Macd2Service
    from app.trading.macd2 import time_window_position_manager as pm
    Macd2Service().set_time_window_x2lite_filter_enabled(True, changed_by="ui")
    s = state_store.load_state()
    mode = tw3.active_3slot_mode(s)
    assert mode == MODE
    ov = tw3.exit_overrides(mode)
    assert ov["tp1_sell_ratio_override"] == pytest.approx(0.20)
    assert ov["trailing_stop_pct_override"] == pytest.approx(2.8)
    assert ov["stop_loss_pct_override"] == pytest.approx(-1.30)
    assert tw3.morning_tp2_pct_override(mode) == pytest.approx(5.0)
    from app.trading.macd2 import early_take_profit as etp
    assert etp.is_enabled(s) is True
    assert etp.thresholds(s) == (pytest.approx(1.5), pytest.approx(1.0))
    d = pm.evaluate_position(session="MORNING", net_return_pct=3.0, tp1_done=False,
                             tp2_pct_override=tw3.morning_tp2_pct_override(mode), **ov)
    assert d.sell_fraction == pytest.approx(0.20)


def test_no_env_var_is_required_for_defaults():
    """추가 env 없이도 W1a 가 켜진 상태로 동작한다."""
    assert config.X2LITE_SIZING_ENABLED is True
    assert config.X2LITE_SIZING_CHOP_MULT == pytest.approx(0.80)
    assert config.X2LITE_SIZING_POST_STOP_MULT == pytest.approx(1.20)
    assert config.X2LITE_SIZING_DAILY_EXPOSURE_CAP == pytest.approx(3.00)


def test_sizing_can_be_disabled_by_env_without_touching_the_strategy(monkeypatch):
    """킬 스위치 — 사이징만 끄고 X2-lite 전략 자체는 그대로 둘 수 있다."""
    s = _x2l()
    monkeypatch.setattr(config, "X2LITE_SIZING_ENABLED", False)
    assert PS.is_active(s) is False
    assert PS.evaluate(s, entry_chop=True).applied == pytest.approx(1.0)
    assert tw3.active_3slot_mode(s) == MODE, "전략 선택은 그대로"


# ════════════════════════════════════════════════════════════════════════════
# E. X2-lite 기본 ON 승격 + 일회성 채택 마이그레이션 (2026-09-12)
# ════════════════════════════════════════════════════════════════════════════
def _write_raw(patch: dict) -> None:
    import json, io as _io
    s = state_store.default_state()
    state_store.save_state(s)
    raw = json.loads(_io.open(state_store.STATE_PATH, encoding="utf-8").read())
    for k in list(raw):
        if k.startswith("time_window_x2lite_") or k.startswith("x2lite_"):
            raw.pop(k, None)
    raw.update(patch)
    _io.open(state_store.STATE_PATH, "w", encoding="utf-8").write(
        json.dumps(raw, ensure_ascii=False))


def test_fresh_state_defaults_to_x2lite():
    s = state_store.default_state()
    assert s.time_window_x2lite_filter_enabled is True
    assert s.time_window_3slot_filter_enabled is False
    assert tw3.active_3slot_mode(s) == MODE
    assert PS.is_active(s) is True


def test_migration_adopts_x2lite_from_a_legacy_tw2_3slot_state():
    """실제 Render state 형태: 3slot=True, x2lite 키 없음 -> 한 번에 X2-lite 로."""
    _write_raw({"time_window_3slot_filter_enabled": True,
                "time_window_twf_filter_enabled": False})
    s = state_store.load_state()
    assert s.time_window_x2lite_filter_enabled is True
    assert s.time_window_3slot_filter_enabled is False
    assert tw3.active_3slot_mode(s) == MODE
    assert PS.is_active(s) is True
    assert s.x2lite_exposure_used_today == pytest.approx(0.0)
    assert s.x2lite_first_trade_stop_loss is False


def test_migration_adopts_x2lite_from_a_legacy_tw_teg_3slot_state():
    _write_raw({"time_window_3slot_filter_enabled": False,
                "time_window_twf_filter_enabled": True})
    s = state_store.load_state()
    assert s.time_window_x2lite_filter_enabled is True
    assert s.time_window_twf_filter_enabled is False
    assert tw3.active_3slot_mode(s) == MODE


def test_migration_runs_only_once_and_respects_a_later_user_choice():
    """마이그레이션 뒤 사용자가 F 를 고르면 그 선택이 유지돼야 한다."""
    from app.trading.macd2.service import Macd2Service
    _write_raw({"time_window_3slot_filter_enabled": True})
    s = state_store.load_state()
    assert tw3.active_3slot_mode(s) == MODE           # 1회 채택
    state_store.save_state(s)                          # 키가 디스크에 생긴다

    Macd2Service().set_time_window_twf_filter_enabled(True, changed_by="ui")
    back = state_store.load_state()
    assert back.time_window_twf_filter_enabled is True
    assert back.time_window_x2lite_filter_enabled is False, "되살아나면 안 된다"
    assert tw3.active_3slot_mode(back) == tw3.MODE_TWF_3SLOT
    assert PS.is_active(back) is False

    again = state_store.load_state()                   # 재시작 모사
    assert again.time_window_twf_filter_enabled is True
    assert again.time_window_x2lite_filter_enabled is False


def test_migration_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setattr(config, "X2LITE_ADOPT_ON_MIGRATION", False)
    _write_raw({"time_window_3slot_filter_enabled": True})
    s = state_store.load_state()
    assert s.time_window_3slot_filter_enabled is True, "저장된 전략 유지"
    assert s.time_window_x2lite_filter_enabled is False


def test_held_position_keeps_its_own_exit_mode_across_migration():
    """마이그레이션 시점에 보유 중이던 포지션은 원래 모드로 청산된다."""
    _write_raw({"time_window_3slot_filter_enabled": True,
                "time_window_position_active": True,
                "time_window_active_mode": tw3.MODE_TW2_3SLOT})
    s = state_store.load_state()
    assert tw3.active_3slot_mode(s) == MODE, "다음 진입부터 X2-lite"
    assert s.time_window_active_mode == tw3.MODE_TW2_3SLOT, "보유 포지션은 원래 모드"
    ov = tw3.exit_overrides(s.time_window_active_mode)
    assert ov["tp1_sell_ratio_override"] is None, "TW2 3-SLOT 청산 그대로"
    assert tw3.morning_tp2_pct_override(s.time_window_active_mode) == pytest.approx(6.0)
