"""진입 후 30분 이내 첫 일반 손절 1봉 유예 (EARLY_STOP_DEFER, 2026-11-06) — worker 경로 계약.

  · MU 무관, 진입 후 경과 <= 30분의 첫 TIME_WINDOW_STOP_LOSS(오전/오후) 만 다음 완성 3분봉 1개 대기
  · 다음 봉도 손절선 아래 -> TIME_WINDOW_STOP_LOSS 청산(deferred=True 기록) / 회복 -> 보유
  · 포지션당 최대 1회 — 어떤 경로로도 2회 유예 불가
  · 대기 중 매 틱 net <= -2.5% -> EARLY_STOP_DEFER_HARD_STOP 즉시 청산, 판정봉이 이미 -2.5% 이하면 유예 없음
  · 기존 MU CONFIRM 진입 1봉 유예(2f00cf0)는 완전히 제거됐다
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.trading.macd2 import config, early_stop_defer, state_store, strategy_mode, worker
from app.trading.macd2.models import RuntimeState
from app.trading.macd2.worker import _net_return_pct, run_once
from tests.macd2.test_early_take_profit_worker import _broker, _market
from tests.macd2.test_fixed_exit_15 import ENTRY, _isolate_shadow_ledger, _sells, _state
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

SYM = config.INVERSE_SYMBOL
STOP = f"{config.EXIT_TW_STOP_LOSS}:{SYM}"
DEFER = f"EARLY_STOP_DEFER:{SYM}"
HARD = f"{config.EXIT_EARLY_STOP_DEFER_HARD_STOP}:{SYM}"
FIELDS = ("early_stop_defer_used", "early_stop_defer_active", "early_stop_defer_started_at",
          "early_stop_defer_until_bar", "early_stop_defer_original_stop_pct")


def _px(net_pct):
    p = ENTRY
    while p > ENTRY * 0.9:
        if _net_return_pct(SYM, ENTRY, p, 10) <= net_pct:
            return p
        p -= 1.0
    raise AssertionError


def _isolate_defer_ledger(monkeypatch, tmp_path):
    led = Path(tmp_path) / "es_ledger.csv"
    monkeypatch.setattr(early_stop_defer, "ledger_path", lambda: led)


def _mk(tmp_path, monkeypatch, *, price, bar_close=None, minutes=10.0, session="MORNING", no_bar=False):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    _isolate_defer_ledger(monkeypatch, tmp_path)
    svc, now = _market(inverse_price=price)
    st = _state(now=now, mode=strategy_mode.MODE_N1, on=False, price=price, bar_close=bar_close,
                entry_minutes_ago=minutes)
    st.time_window_entry_session = session
    if no_bar:                                       # 이번 틱엔 새 완성봉 없음(같은 봉 진행 중)
        st.stop_loss_bar_ts = worker.forming_bar_window(now)[0].isoformat()
    return svc, now, st


def _tick(monkeypatch, svc, now, st, price):
    broker = _broker(price)
    _patch_common(monkeypatch)
    n = len(broker.orders)
    res = run_once(broker=broker, state=st, market_data=svc, now=now)
    return broker.orders[n:], res


def _go(monkeypatch, tmp_path, *, price, bar_close=None, **kw):
    svc, now, st = _mk(tmp_path, monkeypatch, price=price, bar_close=bar_close, **kw)
    orders, res = _tick(monkeypatch, svc, now, st, price)
    return st, orders, res


def _pending(st, monkeypatch, svc, now):
    early_stop_defer.start(st, now=now, until_bar=worker.forming_bar_window(now)[1], original_stop_pct=-1.5)


def _events():
    return [r["event"] for r in early_stop_defer.load_rows()]


# 1 / 2 ─ 경과 10분 · 정확히 30분 -> 유예 ───────────────────────────────────────
@pytest.mark.parametrize("minutes", [10.0, 30.0])
def test_first_stop_within_30min_is_deferred(monkeypatch, tmp_path, minutes):
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, minutes=minutes)
    assert DEFER in res.actions and STOP not in res.actions and _sells(orders) == []
    assert st.early_stop_defer_active and st.early_stop_defer_used
    assert st.early_stop_defer_started_at and st.early_stop_defer_until_bar
    assert st.early_stop_defer_original_stop_pct == pytest.approx(-1.5)
    assert _events() == [early_stop_defer.EVENT_DEFER]
    row = early_stop_defer.load_rows()[0]
    assert float(row["elapsed_min"]) == pytest.approx(minutes) and row["deferred"] == "True"
    assert row["version"] == early_stop_defer.VERSION


# 3 ─ 30분 초과 -> 즉시 손절 ────────────────────────────────────────────────────
def test_stop_after_30min_exits_immediately(monkeypatch, tmp_path):
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, minutes=30.5)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions
    assert not st.early_stop_defer_active
    rows = early_stop_defer.load_rows()
    assert [r["event"] for r in rows] == [early_stop_defer.EVENT_NOT_DEFERRED]
    assert rows[0]["reason"] == early_stop_defer.SKIP_LATE


# 4 ─ 다음 봉 여전히 손절선 아래 -> TIME_WINDOW_STOP_LOSS 청산 ─────────────────────
def test_next_bar_still_below_exits_with_tw_stop(monkeypatch, tmp_path):
    p = _px(-1.7)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _pending(st, monkeypatch, svc, now)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions
    assert _events() == [early_stop_defer.EVENT_STOP_AFTER_DEFER]
    assert early_stop_defer.load_rows()[0]["deferred"] == "True"


# 5 / 6 ─ 다음 봉 회복 -> 보유, 이후 두 번째 손절은 즉시 ─────────────────────────────
def test_recovered_holds_then_second_stop_is_immediate(monkeypatch, tmp_path):
    p = _px(-0.4)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _pending(st, monkeypatch, svc, now)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert _sells(orders) == [] and STOP not in res.actions
    assert st.early_stop_defer_used and not st.early_stop_defer_active
    assert _events() == [early_stop_defer.EVENT_RECOVERED]

    q = _px(-1.6)                                        # 같은 포지션(used=True), 아직 30분 이내
    svc2, now2, st2 = _mk(tmp_path / "2", monkeypatch, price=q, bar_close=q, minutes=12.0)
    st2.early_stop_defer_used = True
    orders2, res2 = _tick(monkeypatch, svc2, now2, st2, q)
    assert STOP in res2.actions and len(_sells(orders2)) == 1 and DEFER not in res2.actions
    assert early_stop_defer.load_rows()[-1]["reason"] == early_stop_defer.SKIP_USED


# 7 ─ 대기 중 hard stop ─────────────────────────────────────────────────────
def test_hard_stop_on_tick_while_waiting(monkeypatch, tmp_path):
    p = _px(-2.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, no_bar=True)
    _pending(st, monkeypatch, svc, now)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert HARD in res.actions and len(_sells(orders)) == 1 and STOP not in res.actions
    assert not st.early_stop_defer_active
    assert _events() == [early_stop_defer.EVENT_HARD_STOP]


def test_tick_above_hard_stop_keeps_waiting(monkeypatch, tmp_path):
    p = _px(-2.0)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, no_bar=True)
    _pending(st, monkeypatch, svc, now)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert _sells(orders) == [] and st.early_stop_defer_active


def test_hard_stop_not_checked_without_active_defer(monkeypatch, tmp_path):
    """유예 중이 아니면 -2.6% 틱이어도 이 규칙의 hard stop 은 나가지 않는다(기존 경로 그대로)."""
    p = _px(-2.6)
    _st, _orders, res = _go(monkeypatch, tmp_path, price=p, no_bar=True)
    assert HARD not in res.actions


# 8 ─ 판정봉이 이미 -2.5% 이하 -> 유예 없음 ─────────────────────────────────────
def test_bar_already_beyond_hard_stop_not_deferred(monkeypatch, tmp_path):
    p = _px(-2.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions
    assert early_stop_defer.load_rows()[0]["reason"] == early_stop_defer.SKIP_ALREADY_HARD


# 9 ─ B3 손절은 대상 아님 ────────────────────────────────────────────────────
def test_b3_stop_is_not_deferred(monkeypatch, tmp_path):
    from tests.macd2.test_fixed_exit_15 import _run
    from app.trading.macd2 import chop_regime
    _isolate_defer_ledger(monkeypatch, tmp_path)
    p = _px(-1.05)
    st, orders, res = _run(monkeypatch, tmp_path, mode=strategy_mode.MODE_P3, on=False, price=p,
                           bar_close=p, regime=chop_regime.REGIME_CHOP)
    assert DEFER not in res.actions and HARD not in res.actions
    assert not st.early_stop_defer_used and not st.early_stop_defer_active


# 10 ─ 손절이 아닌 래더 결정(TP·after-TP1 stop 등)은 손대지 않는다 ─────────────────────
@pytest.mark.parametrize("reason", [config.EXIT_TW_AFTERNOON_TP, config.EXIT_TW_PROFIT_LOCK_STOP,
                                    config.EXIT_TW_BREAKEVEN_STOP, config.EXIT_TW_TP2_FULL])
def test_non_stop_ladder_exits_unchanged(monkeypatch, tmp_path, reason):
    from app.trading.macd2 import time_window_position_manager as tpm

    def fake_eval(**kw):
        return tpm.PositionManagementDecision(reason, 1.0, False, kw.get("peak_net_return", 0.0), "X")
    monkeypatch.setattr(worker.time_window_position_manager, "evaluate_position", fake_eval)
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert DEFER not in res.actions and f"{reason}:{SYM}" in res.actions and len(_sells(orders)) == 1
    assert not st.early_stop_defer_used and early_stop_defer.load_rows() == []


def test_opposite_flag_and_forced_exits_bypass_rule():
    """반대플래그·강제청산·H50·C1 등은 래더 판정 블록 밖에서 execute_exit 를 직접 부른다 —
    이 규칙의 분기는 EXIT_TW_STOP_LOSS 판정 한 곳에만 있으므로 그 경로들에 영향이 없다."""
    src = Path(worker.__file__).read_text(encoding="utf-8")
    assert src.count("early_stop_defer.skip_reason(") == 1
    assert src.count("early_stop_defer.start(") == 1
    i = src.index("early_stop_defer.skip_reason(")
    assert "pm_decision.exit_reason == config.EXIT_TW_STOP_LOSS and pm_decision.sell_fraction >= 1.0" \
        in src[i - 300:i]


# 11 ─ 토글 OFF parity ───────────────────────────────────────────────────────
def test_toggle_off_stops_as_before(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EARLY_STOP_DEFER_ENABLED", False)
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions
    assert all(getattr(st, f) in (False, None) for f in FIELDS)


# 12 ─ 재시작 round-trip ────────────────────────────────────────────────────
def test_state_roundtrip_mid_defer(monkeypatch, tmp_path):
    p = _px(-1.6)
    st, _o, _r = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    r = state_store.deserialize(state_store.serialize(st))
    for f in FIELDS:
        assert getattr(r, f) == getattr(st, f), f
    assert r.early_stop_defer_active and r.early_stop_defer_used


def test_restart_mid_defer_then_next_bar_exits(monkeypatch, tmp_path):
    """유예 중 재시작(직렬화/복원) 후 다음 봉에서 정상 판정되고, 2회째 유예가 안 생긴다."""
    p = _px(-1.7)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _pending(st, monkeypatch, svc, now)
    st = state_store.deserialize(state_store.serialize(st))
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and DEFER not in res.actions and len(_sells(orders)) == 1


# 13 ─ 예전 상태파일 호환 ───────────────────────────────────────────────────
def test_old_state_file_compat():
    raw = state_store.serialize(state_store.default_state())
    for f in FIELDS:
        raw.pop(f, None)
    old = state_store.deserialize(raw)
    assert old.early_stop_defer_used is False and old.early_stop_defer_active is False
    assert old.early_stop_defer_started_at is None and old.early_stop_defer_until_bar is None
    assert old.early_stop_defer_original_stop_pct is None


def test_state_file_with_removed_confirm_keys_still_loads():
    """2f00cf0 시절 상태파일(confirm_stop_defer_* 키 포함)도 읽히고, 그 값은 무시된다."""
    raw = state_store.serialize(state_store.default_state())
    raw.update({"time_window_entry_mu_status": "ENTRY_CONFIRM", "confirm_stop_defer_state": "PENDING",
                "confirm_stop_defer_at": "2026-11-05T10:00:00+09:00"})
    st = state_store.deserialize(raw)
    assert not st.early_stop_defer_active and not hasattr(st, "confirm_stop_defer_state")


# 14 ─ 오후 손절도 동일하게 ───────────────────────────────────────────────────
def test_afternoon_stop_is_deferred_and_original_stop_recorded(monkeypatch, tmp_path):
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, session="AFTERNOON")
    assert DEFER in res.actions and _sells(orders) == []
    assert st.early_stop_defer_original_stop_pct == pytest.approx(config.AFTERNOON_STOP_LOSS * 100)


def test_afternoon_next_bar_still_below_exits(monkeypatch, tmp_path):
    p = _px(-1.7)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p, session="AFTERNOON")
    _pending(st, monkeypatch, svc, now)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and len(_sells(orders)) == 1


# ─ 2회 유예 불가 (전 경로) / 포지션 종료 시 리셋 / CONFIRM 제거 ─────────────────────
def test_no_second_deferral_across_full_sequence(monkeypatch, tmp_path):
    """DEFER -> 회복(보유) -> 재손절(즉시) 을 한 상태 객체로 연속 실행. 유예 액션은 정확히 1번."""
    actions = []
    p1 = _px(-1.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p1, bar_close=p1)
    _o, res = _tick(monkeypatch, svc, now, st, p1)
    actions += res.actions
    assert st.early_stop_defer_active
    for price in (_px(-0.4), _px(-1.6)):                 # 다음 봉 회복, 그다음 봉 재손절
        svc2, now2, st2 = _mk(tmp_path / str(price), monkeypatch, price=price, bar_close=price)
        for f in FIELDS:
            setattr(st2, f, getattr(st, f))
        _o, res = _tick(monkeypatch, svc2, now2, st2, price)
        actions += res.actions
        st = st2
    assert actions.count(DEFER) == 1 and actions.count(STOP) == 1
    # 재손절로 포지션이 닫혔으므로 포지션 종속 상태는 리셋(다음 포지션은 새로 1회)
    assert all(getattr(st, f) in (False, None) for f in FIELDS)


def test_used_flag_alone_blocks_deferral_even_if_active_false():
    s = RuntimeState()
    s.early_stop_defer_used = True
    assert early_stop_defer.skip_reason(s, None, __import__("datetime").datetime.now(), -1.6) \
        == early_stop_defer.SKIP_USED


def test_position_scoped_reset():
    s = RuntimeState()
    s.early_stop_defer_used, s.early_stop_defer_active = True, True
    s.early_stop_defer_started_at = s.early_stop_defer_until_bar = "x"
    s.early_stop_defer_original_stop_pct = -1.5
    worker._clear_position_scoped_state(s, reason="TEST")
    assert all(getattr(s, f) in (False, None) for f in FIELDS)


def test_defaults_and_confirm_defer_fully_removed():
    assert config.EARLY_STOP_DEFER_ENABLED is True
    assert config.EARLY_STOP_DEFER_MINUTES == pytest.approx(30.0)
    assert config.EARLY_STOP_DEFER_HARD_STOP == pytest.approx(-0.025)
    assert config.AFTERNOON_STOP_LOSS == pytest.approx(-0.015)
    for name in ("MU_CONFIRM_STOP_DEFER_ENABLED", "MU_CONFIRM_STOP_DEFER_HARD_PCT"):
        assert not hasattr(config, name)
    s = RuntimeState()
    for name in ("time_window_entry_mu_status", "confirm_stop_defer_state", "confirm_stop_defer_at"):
        assert not hasattr(s, name)
    assert not hasattr(worker, "_confirm_stop_defer_eligible")
    root = Path(worker.__file__).parent
    for f in root.glob("*.py"):
        txt = f.read_text(encoding="utf-8")
        assert "CONFIRM_STOP_DEFER" not in txt and "confirm_stop_defer" not in txt, f.name


def test_hard_stop_counts_as_first_trade_stop_for_post_stop_sizing(monkeypatch):
    """유예 중 hard stop 으로 끝난 첫 거래도 POST_STOP x1.20 의 '첫 거래 손절'로 인정된다.
    (유예 후 다음 봉 손절은 사유가 TIME_WINDOW_STOP_LOSS 그대로라 원래부터 인정된다.)"""
    from app.trading.macd2 import position_sizing
    monkeypatch.setattr(position_sizing, "is_active", lambda st: True)
    for reason in (config.EXIT_EARLY_STOP_DEFER_HARD_STOP, config.EXIT_TW_STOP_LOSS):
        s = RuntimeState()
        s.x2lite_entry_seq_today = 1
        position_sizing.note_full_exit(s, reason)
        assert s.x2lite_first_trade_stop_loss is True, reason
    s = RuntimeState()
    s.x2lite_entry_seq_today = 1
    position_sizing.note_full_exit(s, "B3_MAXHOLD")          # 10/14 B 경로: 손절 아님 -> x1.20 없음
    assert s.x2lite_first_trade_stop_loss is False
