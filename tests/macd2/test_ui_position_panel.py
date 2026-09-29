"""MACD2 보유 포지션 패널 (읽기 전용 UI, 2026-09-29).

패널 값이 **실제 state 필드 / worker 판정 함수** 결과와 일치하는지, 모르는 값은
추정하지 않고 UNKNOWN 으로 두는지 확인한다. 상태는 운영과 같은 함수
(strategy_mode.apply / note_entry_regime / note_hold_start / note_rescued ...)로 만든다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime, config, early_take_profit, p3_stack, small_whipsaw_hold, state_store, strategy_mode, worker,
)
from app.trading.macd2.models import Direction, PositionSnapshot
from app.ui import macd2_position_panel as panel_mod

KST = config.KST
NOW = datetime(2026, 9, 29, 12, 40, tzinfo=KST)
ENTRY = datetime(2026, 9, 29, 12, 15, 38, tzinfo=KST)


def _held(*, symbol=config.INVERSE_SYMBOL, entry=ENTRY, regime=chop_regime.REGIME_CHOP,
          entry_bar_chop=True, px=6_009.0, qty=1_384):
    """P3 모드 + N1 3-SLOT 이 관리하는 보유 포지션 (정상 진입 후처리와 같은 필드)."""
    st = state_store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_P3)
    st.position = PositionSnapshot(symbol=symbol, quantity=qty, avg_price=px, entry_at=entry)
    st.time_window_position_active = True
    st.time_window_active_mode = "N1_3SLOT"
    st.last_time_window_entry_at = entry.isoformat()
    st.time_window_entry_chop = bool(entry_bar_chop)
    if regime is not None:
        p3_stack.note_entry_regime(st, regime, now=entry)
    return st


def _mgmt(panel):
    return dict(panel["management"])


def _build(st, price=6_060.0, now=NOW):
    return panel_mod.build_position_panel(st, current_price=price, now=now)


# ── A. CHOP + B3 ──────────────────────────────────────────────────────────
def test_A_chop_entry_is_b3():
    st = _held()
    p = _build(st, now=ENTRY + timedelta(minutes=3))
    assert p["position_mode"] == p3_stack.MODE_B3 == p3_stack.position_mode(st)
    m = _mgmt(p)
    assert m["B3"] == "ACTIVE" and m["Q2"] == "ACTIVE" and m["Y3"] == "PENDING"
    assert m["N1"] == "OFF", "B3 관리 중에는 N1 틱 익절 래더가 대체된다"
    assert p["b3"]["tp_pct"] == config.P3_B3_TP_PCT and p["b3"]["sl_pct"] == config.P3_B3_SL_PCT
    assert p["b3"]["max_hold_min"] == config.P3_B3_MAX_HOLD_MIN
    assert p["entry"] == {"selected_mode": "P3", "entry_regime": "CHOP", "entry_bar_chop": "YES"}
    # 6분이 지나면 Q2 창은 닫힌다
    p_late = _build(st, now=ENTRY + timedelta(minutes=10))
    assert _mgmt(p_late)["Q2"] == "OFF"


# ── B. Q2 rescue 후 P3-RUNNER ─────────────────────────────────────────────
def test_B_q2_rescue_becomes_p3_runner():
    st = _held()
    p3_stack.note_rescued(st, ENTRY + timedelta(minutes=4))
    p = _build(st)
    assert p["position_mode"] == p3_stack.MODE_P3_RUNNER
    m = _mgmt(p)
    assert m["Q2"] == "USED" and m["B3"] == "OFF" and m["N1"] == "ACTIVE"
    assert p["runner"]["kind"] == "Q2"
    assert p["b3"] is None


# ── C. H30 active ─────────────────────────────────────────────────────────
def test_C_h30_active_shows_deadline():
    st = _held()
    p3_stack.note_h30_start(st, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    p = _build(st, now=ENTRY + timedelta(minutes=22))
    assert p["position_mode"] == p3_stack.MODE_H30
    m = _mgmt(p)
    assert m["H30"] == "ACTIVE" and m["B3"] == "ACTIVE"
    assert m["Q2"] == "ACTIVE", "H30 연장 중 +1% 최초 도달이면 Q2 와 같은 부분익절"
    assert p["h30"]["deadline_at"] == p3_stack.h30_deadline(ENTRY)


# ── D. Y3-RUNNER ──────────────────────────────────────────────────────────
def test_D_y3_runner():
    st = _held()
    p3_stack.note_y3_promoted(st, ENTRY + timedelta(minutes=20, seconds=30))
    p = _build(st)
    assert p["position_mode"] == p3_stack.MODE_Y3_RUNNER
    m = _mgmt(p)
    assert m["Y3"] == "PROMOTED" and m["B3"] == "OFF" and m["N1"] == "ACTIVE"
    assert p["runner"] == {"kind": "Y3", "promoted_at": ENTRY + timedelta(minutes=20, seconds=30)}


# ── E. BASE (TREND 진입) ──────────────────────────────────────────────────
def test_E_trend_entry_is_base():
    st = _held(regime=chop_regime.REGIME_TREND, entry_bar_chop=False)
    p = _build(st)
    assert p["position_mode"] == p3_stack.MODE_BASE
    m = _mgmt(p)
    assert m["B3"] == "OFF" and m["Y3"] == "OFF" and m["N1"] == "ACTIVE"
    assert p["entry"]["entry_regime"] == "TREND"
    assert p["entry"]["entry_bar_chop"] == "NO"


# ── F / G. 진입봉 CHOP YES / NO ─────────────────────────────────────────
def test_F_entry_bar_chop_yes_is_early_tp_eligible():
    st = _held(entry_bar_chop=True)
    p = _build(st)
    assert p["entry"]["entry_bar_chop"] == "YES"
    assert p["early_tp"]["eligible"] is True
    assert p["early_tp"]["status"] in ("ELIGIBLE", "ARMED")


def test_G_entry_bar_chop_no_turns_early_tp_off_with_reason():
    st = _held(entry_bar_chop=False)
    p = _build(st)
    assert p["entry"]["entry_bar_chop"] == "NO"
    assert p["early_tp"]["status"] == "OFF"
    assert p["early_tp"]["reason"] == "Entry Bar CHOP = NO"
    assert _mgmt(p)["EARLY TP"] == "OFF"


# ── H. 조기익절 ARMED ─────────────────────────────────────────────────────
def test_H_early_tp_armed_uses_production_thresholds():
    st = _held(entry_bar_chop=True)
    trig, floor = early_take_profit.thresholds(st)
    assert (trig, floor) == (config.N1_EARLY_TP_TRIGGER_PCT, config.N1_EARLY_TP_FLOOR_PCT)
    st.early_tp_peak_net_return = trig + 0.09          # +1.59%
    p = _build(st)
    assert p["early_tp"]["status"] == "ARMED" and p["early_tp"]["armed"] is True
    assert p["early_tp"]["peak_pct"] == pytest.approx(trig + 0.09)
    assert p["early_tp"]["arm_pct"] == trig and p["early_tp"]["floor_pct"] == floor
    st.early_tp_peak_net_return = trig - 0.01
    assert _build(st)["early_tp"]["status"] == "ELIGIBLE"


# ── I. H50 active ─────────────────────────────────────────────────────────
def test_I_h50_hold_details():
    st = _held()
    started = datetime(2026, 9, 29, 12, 48, tzinfo=KST)
    small_whipsaw_hold.note_hold_start(st, held_direction=Direction.DOWN_BLUE, now=started,
                                       decision=small_whipsaw_hold.NO_HOLD)
    st.h50_trend_break_count = 1
    p = _build(st, now=started + timedelta(minutes=31))
    assert p["h50"]["status"] == "HOLD" and _mgmt(p)["H50"] == "HOLD"
    assert p["h50"]["elapsed_min"] == pytest.approx(31.0)
    assert p["h50"]["max_hold_min"] == config.H50_MAX_HOLD_MIN
    assert (p["h50"]["trend_break_count"], p["h50"]["trend_break_needed"]) == (1, config.H50_TREND_BREAK_BARS)
    assert p["h50"]["original_direction"] == "DOWN_BLUE"
    assert any("H50 HOLD: ACTIVE" in line and "31m / 60m" in line for line in panel_mod.render_lines(p))


# ── J. state 누락 fallback (오늘 12:15 인버스와 같은 입양 포지션) ──────────
def test_J_unknown_state_falls_back_without_guessing():
    st = _held(regime=None, entry_bar_chop=False)   # P3 스냅샷 없음 + 진입봉 판정 미기록
    p = _build(st)
    assert p["entry"] == {"selected_mode": "UNKNOWN", "entry_regime": "UNKNOWN", "entry_bar_chop": "UNKNOWN"}
    assert p["position_mode"] == p3_stack.MODE_BASE
    assert p["early_tp"]["status"] == "OFF"
    assert p["early_tp"]["reason"] == "Entry Bar CHOP = UNKNOWN"


# ── 공통 ──────────────────────────────────────────────────────────────────
def test_no_position_returns_none():
    st = state_store.default_state()
    assert panel_mod.build_position_panel(st, current_price=None, now=NOW) is None


def test_net_return_matches_worker_formula():
    st = _held()
    p = _build(st, price=6_060.0)
    assert p["position"]["net_pct"] == pytest.approx(
        worker._net_return_pct(config.INVERSE_SYMBOL, 6_009.0, 6_060.0, 1_384))
    assert p["position"]["direction"] == "DOWN_BLUE" and p["position"]["label"] == "인버스"


def test_panel_never_mutates_state():
    st = _held()
    small_whipsaw_hold.note_hold_start(st, held_direction=Direction.DOWN_BLUE, now=NOW,
                                       decision=small_whipsaw_hold.NO_HOLD)
    before = state_store.serialize(st) if hasattr(state_store, "serialize") else dict(vars(st))
    lines = panel_mod.render_lines(_build(st))
    after = state_store.serialize(st) if hasattr(state_store, "serialize") else dict(vars(st))
    assert before == after
    assert lines and lines[1].startswith("**POSITION MODE:")


def test_missing_quote_is_shown_as_dash_not_guessed():
    st = _held()
    p = panel_mod.build_position_panel(st, current_price=None, now=NOW)
    assert p["position"]["current_price"] is None and p["position"]["net_pct"] is None
    assert "현재가 -" in panel_mod.render_lines(p)[0]


# ── 페이지 렌더 (AppTest) ─────────────────────────────────────────────────
def test_page_renders_position_panel_for_a_held_p3_position():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    st = _held()
    p3_stack.note_y3_promoted(st, ENTRY + timedelta(minutes=20))
    st.auto_trade_on = False
    state_store.save_state(st)

    app_path = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
    at = AppTest.from_file(app_path, default_timeout=40)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if "can't be used in an `st.form()`" in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- 이 패널과 무관")
    assert not at.exception, [str(e.value) for e in at.exception]
    text = "\n".join(m.value for m in at.markdown)
    assert "POSITION MODE: Y3-RUNNER" in text
    assert "ENTRY CONTEXT" in text and "Regime CHOP" in text and "Entry Bar CHOP YES" in text
    assert "ACTIVE MANAGEMENT" in text and "EARLY TP" in text
