"""2026-09-29 실사고 회귀 — POSITION_DATA_ERROR 뒤 재시도 체결이 3-SLOT 후처리를
통째로 건너뛰던 버그 + P3 섀도우 ``.chop`` AttributeError.

실사고: P3 모드, REGIME=CHOP 인데 10:36 레버리지 / 12:15 인버스 두 거래가
모두 POSITION MODE=BASE. 두 거래 모두 주문 직전 잔고조회 실패로 신호원장에
``WAITING / POSITION_DATA_ERROR`` 가 먼저 찍히고, 이후 run_once 의 pending
재시도가 실제로 체결했다. 그 재시도 경로는

  * P3 note_entry_regime (B3 ownership) / position_epoch
  * 3-SLOT 슬롯·오전오후 카운트 (하루 3회 cap 의 입력)
  * W1a/x2lite 일일 노출 note_entry (노출 3.0 의 입력)

을 전부 건너뛰었고, 신호원장은 signal_id dedup 때문에 체결 행을 버렸다.
또 ``_p3_note_shadow_flag`` 의 ``.chop`` 오타로 섀도우 BASE 가 확정 플래그를 한 번도
받지 못해 detector 가 seed 장부에 고정돼 있었다.

전부 실제 ``worker.run_once()`` 경로를 탄다(fake broker, conftest 격리).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime, config, ledger, p3_stack, position_sizing, shadow_base, state_store,
    strategy_mode, time_window_3slot, worker,
)
from app.trading.macd2.models import Direction
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _market
from tests.macd2.test_p3_worker import _isolate_shadow_ledger, _ledger_before
from tests.macd2.test_tw2_3slot_worker_regression import (
    _PRIOR_DAY, _approved, _fresh_3slot_state, _patch_common, _prime_3slot_pending, _quality, _teg,
)

KST = config.KST

# 오늘 실거래 가격 (거래원장)
LEVERAGE_1036 = 10_665.0
INVERSE_1215 = 6_009.0


# ── 하네스 ────────────────────────────────────────────────────────────────
def _setup(monkeypatch, tmp_path, *, long_price=15_000.0, inverse_price=10_000.0, h50=5, tp1=0):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=inverse_price, long_price=long_price)
    chop_regime.save_ledger(_ledger_before(now0, h50=h50, tp1=tp1), source="seed-test")
    state = _fresh_3slot_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.mode = "mock"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: long_price,
                                                   config.INVERSE_SYMBOL: inverse_price})
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True),
                  teg_decision=_teg(True))
    return svc, now0, state, broker


def _fail_pre_order_balance_queries(monkeypatch, n: int = 1):
    """**주문 직전** 강제 reconcile(``_execute_or_wait`` 의 ``force=True``) 처음 ``n`` 회만
    KIS 잔고조회를 실패시킨다 -- 오늘 운영의 POSITION_DATA_ERROR 와 같은 자리다.
    실제 ``reconcile_position_state`` 를 그대로 태우고 브로커 조회만 예외로 만든다."""
    real_reconcile = worker.reconcile_position_state
    left = {"n": int(n)}

    def wrapped(broker_, state_, now_, *, force=False):
        if force and left["n"] > 0:
            left["n"] -= 1
            real_get = broker_.get_positions

            def boom(*a, **k):
                raise RuntimeError("KIS 실계좌 잔고 조회 실패: simulated (2026-09-29)")

            broker_.get_positions = boom
            try:
                return real_reconcile(broker_, state_, now_, force=force)
            finally:
                broker_.get_positions = real_get
        return real_reconcile(broker_, state_, now_, force=force)

    monkeypatch.setattr(worker, "reconcile_position_state", wrapped)


def _buys(broker):
    return [o for o in broker.orders if o.side == "BUY" and o.success]


def _signal_rows(signal_id):
    return [r for r in ledger._load_rows(ledger.SIGNAL_LEDGER_PATH, limit=0)
            if r.get("signal_id") == signal_id]


def _run_error_then_retry(monkeypatch, svc, now0, state, broker, direction, *, before_retry=None):
    """오늘 운영과 같은 흐름을 **한 tick** 안에서 재현한다.

    1. 3-SLOT T+3 승인 -> 주문 직전 잔고조회 실패 -> WAITING + pending
    2. 같은 tick 끝의 run_once pending 재시도가 곧바로 다시 주문 -> 체결
       (2026-09-29 10:36 / 12:15 가 정확히 이 경로였다)

    ``before_retry`` 는 1과 2 사이에 state 를 바꿔 보는 훅이다(cap/노출 테스트).
    MACD 방향 유지(``_pending_direction_still_active``)는 운영과 같게 참으로 둔다.
    """
    monkeypatch.setattr(worker, "_pending_direction_still_active", lambda d, s: True)
    _prime_3slot_pending(state, direction, before=_PRIOR_DAY)
    _fail_pre_order_balance_queries(monkeypatch, n=1)
    seen = {}
    real_retry = worker._retry_pending_signal

    def spy(**kw):
        pend = dict(kw["state"].pending_signal or {})
        seen["signal_id"] = pend.get("signal_id")
        seen["ctx"] = pend.get("tw2_3slot_ctx")
        seen["rows_before"] = [r["order_result"] for r in _signal_rows(pend.get("signal_id"))]
        if before_retry is not None:
            before_retry(kw["state"])
        return real_retry(**kw)

    monkeypatch.setattr(worker, "_retry_pending_signal", spy)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert seen.get("ctx"), "3-SLOT 첫 시도가 pending 에 진입 문맥을 실어야 한다"
    assert seen["rows_before"] == ["WAITING"], seen["rows_before"]
    return state, seen["signal_id"], r


# ── A. POSITION_DATA_ERROR -> 재시도 체결: 최초 체결과 같은 후처리 ─────────
def test_retry_fill_gets_full_3slot_post_entry(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    epoch_before = int(state.position_epoch or 0)
    state, signal_id, r2 = _run_error_then_retry(monkeypatch, svc, now0, state, broker, Direction.UP_RED)

    assert r2.skipped == worker.POSITION_DATA_ERROR          # 첫 시도는 막혔고
    assert f"ENTRY:{Direction.UP_RED.value}" in r2.actions, r2.actions   # 같은 tick 재시도가 체결
    assert len(_buys(broker)) == 1, "실제 BUY 는 정확히 1회"
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    # P3 ownership
    assert state.p3_entry_regime == chop_regime.REGIME_CHOP
    assert state.p3_position_active is True
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    assert p3_stack.governs_position(state) is True
    # 3-SLOT / W1a 집계
    assert state.tw2_3slot_slots_used_today == 1
    assert state.tw2_3slot_morning_count + state.tw2_3slot_afternoon_count == 1
    assert state.x2lite_entry_seq_today == 1
    assert state.x2lite_exposure_used_today == pytest.approx(state.x2lite_last_applied_sizing)
    assert state.x2lite_exposure_used_today > 0
    assert int(state.position_epoch) == epoch_before + 1
    assert state.time_window_position_active is True
    assert state.time_window_active_mode == time_window_3slot.MODE_N1_3SLOT
    assert state.tw2_3slot_post_entry_signal_id == signal_id
    assert state.pending_signal is None


def test_retry_uses_the_same_sizing_multiplier_as_the_first_attempt(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    state, signal_id, _ = _run_error_then_retry(monkeypatch, svc, now0, state, broker, Direction.UP_RED)
    # 최초 판정의 W1a 배수가 그대로(노출 잔여 3.0 안이므로 재절단 없음) 쓰였다.
    ctx_applied = state.x2lite_last_applied_sizing
    assert ctx_applied == pytest.approx(state.x2lite_exposure_used_today)


# ── 중복 후처리 0 / 재시작 ───────────────────────────────────────────────
def test_post_entry_is_idempotent_for_the_same_fill(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    state, signal_id, _ = _run_error_then_retry(monkeypatch, svc, now0, state, broker, Direction.UP_RED)
    snap = (state.tw2_3slot_slots_used_today, state.x2lite_exposure_used_today,
            state.x2lite_entry_seq_today, state.position_epoch, state.p3_entry_regime)
    ran = worker._finalize_tw2_3slot_entry(
        state, outcome=None, direction=Direction.UP_RED, now=now0, session="MORNING",
        sizing=position_sizing.NEUTRAL, presized_chop=None, bars_3m=None,
        signal_detected_at=now0, signal_id=signal_id,
    )
    assert ran is False
    assert (state.tw2_3slot_slots_used_today, state.x2lite_exposure_used_today,
            state.x2lite_entry_seq_today, state.position_epoch, state.p3_entry_regime) == snap
    # 다음 tick 에서도 다시 세지 않는다.
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=40))
    assert state.tw2_3slot_slots_used_today == 1
    assert len(_buys(broker)) == 1


def test_pending_context_and_ownership_survive_restart(monkeypatch, tmp_path):
    """같은 tick 재시도도 실패해 pending 이 남은 채 **재시작**해도, 다음 tick 의
    재시도가 같은 후처리를 탄다. 체결 뒤 재시작해도 ownership/카운트가 유지된다."""
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "_pending_direction_still_active", lambda d, s: True)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    _fail_pre_order_balance_queries(monkeypatch, n=2)       # 첫 시도 + 같은 tick 재시도 모두 실패
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == [] and state.pending_signal
    signal_id = state.pending_signal["signal_id"]
    state_store.save_state(state)
    state = state_store.load_state()                        # 재시작
    assert state.pending_signal.get("tw2_3slot_ctx"), "재시작 후에도 pending 문맥 유지"
    t2 = now0 + timedelta(seconds=10)
    state.last_position_reconcile_at = (t2 - timedelta(seconds=31)).isoformat()  # 30초 간격 경과
    run_once(broker=broker, market_data=svc, state=state, now=t2)
    assert len(_buys(broker)) == 1
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    assert [r["order_result"] for r in _signal_rows(signal_id)] == ["EXECUTED"]
    state_store.save_state(state)
    after = state_store.load_state()                        # 체결 뒤 재시작
    assert after.p3_position_active is True and after.p3_entry_regime == chop_regime.REGIME_CHOP
    assert after.tw2_3slot_slots_used_today == 1
    assert after.x2lite_exposure_used_today == pytest.approx(state.x2lite_exposure_used_today)
    assert after.tw2_3slot_post_entry_signal_id == signal_id
    assert p3_stack.position_mode(after) == p3_stack.MODE_B3


# ── 안전성: 하루 3회 / 노출 3.0 ──────────────────────────────────────────
def test_retry_respects_daily_three_trade_cap(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)

    def fill_slots(s):
        s.tw2_3slot_slots_used_today = int(config.TW2_3SLOT_DAILY_CAP)

    state, signal_id, r2 = _run_error_then_retry(monkeypatch, svc, now0, state, broker,
                                                 Direction.UP_RED, before_retry=fill_slots)
    assert _buys(broker) == []
    assert state.position is None
    assert state.pending_signal is None
    assert state.tw2_3slot_slots_used_today == int(config.TW2_3SLOT_DAILY_CAP)


def test_retry_recaps_sizing_to_remaining_daily_exposure(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)

    def use_exposure(s):
        s.x2lite_exposure_used_today = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - 0.5

    state, signal_id, _ = _run_error_then_retry(monkeypatch, svc, now0, state, broker,
                                                Direction.UP_RED, before_retry=use_exposure)
    assert len(_buys(broker)) == 1
    assert state.x2lite_last_applied_sizing == pytest.approx(0.5)
    assert state.x2lite_exposure_used_today == pytest.approx(float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP))


# ── B. 신호원장: WAITING -> EXECUTED 로 갱신 ───────────────────────────────
def test_signal_ledger_waiting_row_becomes_executed(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    state, signal_id, _ = _run_error_then_retry(monkeypatch, svc, now0, state, broker, Direction.UP_RED)
    rows = _signal_rows(signal_id)
    assert len(rows) == 1, "signal_id 당 한 행 원칙은 유지"
    assert rows[0]["order_result"] == "EXECUTED"
    assert rows[0]["broker_order_id"]
    # 정체성 컬럼(어느 봉의 플래그였나)은 최초 WAITING 행 값 그대로
    assert rows[0]["completed_bar_at"]


def test_final_signal_rows_are_never_overwritten(tmp_path):
    base = {"signal_id": "SID_FINAL", "order_result": "EXECUTED", "completed_bar_at": "100000"}
    assert ledger.append_signal(dict(base)) is True
    assert ledger.append_signal({**base, "order_result": "FAILED"}) is False
    assert ledger.append_signal({**base, "order_result": "WAITING"}) is False
    rows = _signal_rows("SID_FINAL")
    assert [r["order_result"] for r in rows] == ["EXECUTED"]


def test_waiting_row_is_not_replaced_by_another_waiting(tmp_path):
    base = {"signal_id": "SID_WAIT", "order_result": "WAITING", "block_reason": "POSITION_DATA_ERROR"}
    assert ledger.append_signal(dict(base)) is True
    assert ledger.append_signal({**base, "block_reason": "OTHER"}) is False
    assert _signal_rows("SID_WAIT")[0]["block_reason"] == "POSITION_DATA_ERROR"


# ── 정상 최초 체결(오류 없음) 경로 — 기존과 동일 ────────────────────────────
def test_normal_first_fill_is_unchanged(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert r.actions == [f"TW2_3SLOT_ENTRY:{config.LONG_SYMBOL}"]
    assert len(_buys(broker)) == 1
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    assert state.tw2_3slot_slots_used_today == 1
    assert state.x2lite_entry_seq_today == 1
    assert state.pending_signal is None
    sid = state.tw2_3slot_post_entry_signal_id
    assert [r["order_result"] for r in _signal_rows(sid)] == ["EXECUTED"]


# ── 오늘 anchor: 10:36 레버리지 / 12:15 인버스 ─────────────────────────────
@pytest.mark.parametrize("label, direction, symbol, long_px, inv_px", [
    ("10:36 leverage", Direction.UP_RED, config.LONG_SYMBOL, LEVERAGE_1036, INVERSE_1215),
    ("12:15 inverse", Direction.DOWN_BLUE, config.INVERSE_SYMBOL, LEVERAGE_1036, INVERSE_1215),
])
def test_today_anchor_retry_fill_is_b3(monkeypatch, tmp_path, label, direction, symbol, long_px, inv_px):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path, long_price=long_px, inverse_price=inv_px)
    state, signal_id, _ = _run_error_then_retry(monkeypatch, svc, now0, state, broker, direction)
    assert state.position is not None and state.position.symbol == symbol, label
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3, f"{label}: BASE 가 아니라 B3 여야 한다"
    assert state.p3_entry_regime == chop_regime.REGIME_CHOP
    assert state.tw2_3slot_slots_used_today == 1
    assert [r["order_result"] for r in _signal_rows(signal_id)] == ["EXECUTED"]


# ── C/D. 섀도우 detector: .chop AttributeError 제거 + recent10 이동 ────────
def test_shadow_flag_no_longer_raises_and_opens_shadow_position(monkeypatch, tmp_path):
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(worker.logger, "exception", lambda *a, **k: errors.append(a))
    bars_3m, _ = worker.filter_complete_3m_bars(
        worker.resample_completed_3m(svc.get_history_df(), now=now0), svc.get_history_df())
    worker._p3_note_shadow_flag(state=state, now=now0, market_data=svc, bars_3m=bars_3m,
                                direction=Direction.UP_RED, base_cleared=True, flag_bar_dt=_PRIOR_DAY)
    assert errors == [], f"섀도우 플래그 처리 예외: {errors!r}"
    assert shadow_base.load_book(state).position is not None, "섀도우 BASE 가 플래그를 받아 진입해야 한다"


def test_shadow_completed_trade_moves_recent10_and_recomputes_regime(monkeypatch, tmp_path):
    # seed: H50 4/10 (=0.40, 경계값) · TP1 0/10 -> CHOP. 가장 오래된 4건이 H50 개입.
    svc, now0, state, broker = _setup(monkeypatch, tmp_path, h50=4, tp1=0)
    before = chop_regime.current_regime(as_of=now0.isoformat())
    assert before.regime == chop_regime.REGIME_CHOP and before.sample == 10
    monkeypatch.setattr(worker.logger, "exception", lambda *a, **k: (_ for _ in ()).throw(AssertionError(a)))
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_hold",
                        lambda *a, **k: worker.small_whipsaw_hold.NO_HOLD)
    bars_3m, _ = worker.filter_complete_3m_bars(
        worker.resample_completed_3m(svc.get_history_df(), now=now0), svc.get_history_df())
    # 섀도우 진입(UP) -> 반대 플래그(DOWN)로 OPPOSITE 청산 -> 완료거래 1건 추가
    worker._p3_note_shadow_flag(state=state, now=now0, market_data=svc, bars_3m=bars_3m,
                                direction=Direction.UP_RED, base_cleared=True, flag_bar_dt=_PRIOR_DAY)
    t1 = now0 + timedelta(minutes=6)
    worker._p3_note_shadow_flag(state=state, now=t1, market_data=svc, bars_3m=bars_3m,
                                direction=Direction.DOWN_BLUE, base_cleared=True, flag_bar_dt=_PRIOR_DAY)
    trades = chop_regime.load_ledger()
    assert len(trades) == 11, "섀도우 완료거래가 ledger 에 추가돼야 한다"
    after = chop_regime.current_regime(as_of=(t1 + timedelta(minutes=1)).isoformat())
    # 창(최근 10건)이 한 칸 밀려 가장 오래된 H50 개입 1건이 빠졌다: 4/10 -> 3/10
    assert after.window == 10 and after.sample == 11
    assert after.h50_rate == pytest.approx(0.3)
    assert after.regime == chop_regime.REGIME_TREND


def test_shadow_flag_uses_is_chop_attribute():
    """재발 방지: EntryChopDecision 의 필드는 is_chop 이다 (chop 아님)."""
    import inspect
    from app.trading.macd2 import early_take_profit
    assert "is_chop" in early_take_profit.EntryChopDecision.__dataclass_fields__
    assert "chop" not in early_take_profit.EntryChopDecision.__dataclass_fields__
    src = inspect.getsource(worker._p3_note_shadow_flag)
    assert ".is_chop" in src and ").chop)" not in src
