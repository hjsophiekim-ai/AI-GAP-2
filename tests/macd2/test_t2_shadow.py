"""T2 SHADOW (2026-10-28) — STRONG 후보 T+2 가상 조기진입 기록. **기록 전용** 계약을 고정한다.

  · STRONG = MU ENTRY_CONFIRM ∧ 체결강도 MATCH 이면 PASS, MU 판정은 됐지만 아니면 FAIL, MU 판정 불가면 기록 안 함
  · T2_ENTRY_PRICE = T+2 현재가 + 1호가, 플래그당 1행(중복 기록 없음)
  · T+3 결과: 체결 -> T3 가격·Δbp·WHIPSAW False / 거절 -> WHIPSAW True / WAITING(E 대기) -> None / E 돌파 체결이 덮어씀
  · 가상 결과: 순수익 +1.5 / −1.0 먼저 닿은 쪽, 30분 뒤 미도달 = NONE_30M, MFE/MAE
  · state·주문에 영향 없음: run_once 결과가 T2 SHADOW ON/OFF 에서 같다
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import config, exec_strength, mu_assist, order_executor, state_store, t2_shadow, worker
from app.trading.macd2.models import Direction, SignalState
from app.trading.macd2.worker import run_once

KST = config.KST
F = datetime(2026, 10, 26, 12, 33, 0, tzinfo=KST)          # 플래그봉 시작
T2 = F + timedelta(minutes=5, seconds=20)                    # T+2 창(F+5 ~ F+6)
SID = "20261026_123300_UP_RED:TW2_3SLOT_CONFIRM"
LONG = config.LONG_SYMBOL


@pytest.fixture(autouse=True)
def _fresh():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def _snaps(root: Path, cttr: float, now: datetime = T2) -> None:
    d = root / "snapshots" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    base = now.replace(tzinfo=None) - timedelta(seconds=60)
    rows = ["ts,symbol,price,cttr"] + [f"{(base + timedelta(seconds=i)).isoformat()},660,1,{cttr}" for i in range(60)]
    (d / "000660.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _pending_state():
    s = state_store.default_state()
    s.mu_assist_enabled = True
    s.tw2_3slot_pending_flag_direction = Direction.UP_RED
    s.tw2_3slot_pending_flag_bar_ts = F.isoformat()
    return s


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_STRENGTH_DATA_DIR", str(tmp_path))
    _snaps(tmp_path, 120.0)
    status = {"v": mu_assist.ENTRY_CONFIRM}
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: status["v"])
    return tmp_path, status


def _outcome(state, *, price=None, reason=None, qty=900):
    return order_executor.ExecutionOutcome(
        SID, Direction.UP_RED, LONG, state, quantity=qty if price else 0,
        filled_avg_price=price or 0.0, block_reason=reason,
        timestamps={"buy_requested_at": (F + timedelta(minutes=6)).isoformat()})


# ── T+2 기록 ────────────────────────────────────────────────────────────────

def test_strong_candidate_is_recorded_as_pass_with_ask_price(world):
    st = _pending_state()
    before = dataclasses.asdict(st)
    worker._advance_t2_strong_shadow(state=st, now=T2, quotes={LONG: 5_300.0})
    assert dataclasses.asdict(st) == before, "state 를 바꾸지 않는다"
    rows = t2_shadow.rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["T2_SHADOW"] == t2_shadow.PASS and r["T2_ENTRY_PRICE"] == 5_305.0
    assert r["flag_id"] == "20261026_123300_UP_RED" and r["symbol"] == LONG and r["done"] is False
    worker._advance_t2_strong_shadow(state=st, now=T2 + timedelta(seconds=5), quotes={LONG: 5_400.0})
    assert len(t2_shadow.rows()) == 1 and t2_shadow.rows()[0]["T2_ENTRY_PRICE"] == 5_305.0, "플래그당 1행"


def test_cttr_mismatch_is_fail_without_virtual_entry(world):
    root, _ = world
    _snaps(root, 95.0)                                  # RED 인데 cttr < 100
    worker._advance_t2_strong_shadow(state=_pending_state(), now=T2, quotes={LONG: 5_300.0})
    r = t2_shadow.rows()[0]
    assert r["T2_SHADOW"] == t2_shadow.FAIL and r["T2_ENTRY_PRICE"] is None and r["done"] is True


@pytest.mark.parametrize("status", ["", mu_assist.ENTRY_MU_UNAVAILABLE])
def test_no_row_when_mu_not_judged(world, status):
    world[1]["v"] = status
    worker._advance_t2_strong_shadow(state=_pending_state(), now=T2, quotes={LONG: 5_300.0})
    assert t2_shadow.rows() == []


def test_outside_t2_window_does_nothing(world):
    worker._advance_t2_strong_shadow(state=_pending_state(), now=F + timedelta(minutes=4), quotes={LONG: 5_300.0})
    worker._advance_t2_strong_shadow(state=_pending_state(), now=F + timedelta(minutes=6, seconds=1),
                                     quotes={LONG: 5_300.0})
    assert t2_shadow.rows() == []


def test_kill_switch(world, monkeypatch):
    monkeypatch.setattr(config, "T2_SHADOW_ENABLED", False)
    worker._advance_t2_strong_shadow(state=_pending_state(), now=T2, quotes={LONG: 5_300.0})
    assert t2_shadow.rows() == []


# ── T+3 결과 ────────────────────────────────────────────────────────────────

def _recorded(world):
    worker._advance_t2_strong_shadow(state=_pending_state(), now=T2, quotes={LONG: 5_300.0})


def test_t3_fill_sets_price_delta_and_not_whipsaw(world):
    _recorded(world)
    t2_shadow.note_signal_result(SID, _outcome(SignalState.EXECUTED, price=5_330.0), F + timedelta(minutes=6))
    r = t2_shadow.rows()[0]
    assert r["T3_ENTRY_PRICE"] == 5_330.0 and r["t3_qty"] == 900
    assert r["T2_vs_T3_DELTA_BP"] == pytest.approx((5_330.0 / 5_305.0 - 1) * 1e4, abs=0.01)
    assert r["T2_WHIPSAW"] is False


def test_t3_rejection_is_whipsaw(world):
    """T+3 거절은 outcome 없이 신호원장 FILTERED_OUT 행으로만 남는다 -- 실제 worker 기록 경로로 확인."""
    from app.trading.macd2.models import MacdSnapshot
    _recorded(world)
    snap = MacdSnapshot(bar_dt=F + timedelta(minutes=3), macd=1.0, signal=1.0, hist=0.0, hist_last3=(0.0, 0.0, 0.0),
                        completed_3m_count=40, previous_diff=0.0, current_diff=0.0, relation="EQUAL")
    st = _pending_state()
    st.order_block_reason = "REJECT_NOT_CONFIRMED"
    worker._record_signal_ledger(st, snap, Direction.UP_RED, "TW2_3SLOT_CONFIRM", SID, F + timedelta(minutes=6),
                                 None, {"order_result_override": "FILTERED_OUT"})
    r = t2_shadow.rows()[0]
    assert r["t3_result"] == "FILTERED_OUT" and r["t3_reason"] == "REJECT_NOT_CONFIRMED"
    assert r["T2_WHIPSAW"] is True and r["t3_block_kind"] == t2_shadow.BLOCK_CONFIRM and r["T3_ENTRY_PRICE"] is None


def test_other_gate_rejection_is_gate_not_whipsaw_and_excluded(world):
    """슬롯 한도·오전 no-trade 같은 게이트는 T+2 였어도 막았다 -> 휩쏘 아님, 가상 손익 집계 제외."""
    _recorded(world)
    t2_shadow.note_signal_result(SID, None, F + timedelta(minutes=6), order_result="FILTERED_OUT",
                                 block_reason="TW2_3SLOT_REJECT_DAILY_SLOT_CAP")
    t2_shadow.update(T2 + timedelta(minutes=2), {LONG: 5_250.0})
    r = t2_shadow.rows()[0]
    assert r["T2_WHIPSAW"] is False and r["t3_block_kind"] == t2_shadow.BLOCK_GATE
    s = t2_shadow.summary()
    assert (s["pass"], s["pass_gate_blocked"], s["pass_effective"], s["hit_sl"], s["whipsaw"]) == (1, 1, 0, 0, 0)


def test_e_wait_is_unknown_then_breakout_fill_overrides(world):
    _recorded(world)
    t2_shadow.note_signal_result(SID, None, F + timedelta(minutes=6))            # WAITING (E 돌파대기)
    assert t2_shadow.rows()[0]["T2_WHIPSAW"] is None
    t2_shadow.note_signal_result(SID + ":E_BRK", _outcome(SignalState.FAILED, reason="X"), F + timedelta(minutes=8))
    assert t2_shadow.rows()[0]["T2_WHIPSAW"] is None, "돌파 미체결은 덮지 않는다"
    t2_shadow.note_signal_result(SID + ":E_BRK", _outcome(SignalState.EXECUTED, price=5_340.0), F + timedelta(minutes=9))
    r = t2_shadow.rows()[0]
    assert r["t3_result"].startswith("E_BRK:") and r["T3_ENTRY_PRICE"] == 5_340.0 and r["T2_WHIPSAW"] is False


def test_unrelated_signal_ids_are_ignored(world):
    _recorded(world)
    t2_shadow.note_signal_result("20261026_123300_UP_RED", _outcome(SignalState.EXECUTED, price=1.0), T2)
    assert t2_shadow.rows()[0]["T3_ENTRY_PRICE"] is None


# ── 가상 결과 추적 ──────────────────────────────────────────────────────────

def test_virtual_tp_first_then_mfe_mae_and_done_after_30m(world):
    _recorded(world)                                     # 진입 5,305
    t2_shadow.update(T2 + timedelta(minutes=1), {LONG: 5_290.0})
    t2_shadow.update(T2 + timedelta(minutes=5), {LONG: 5_390.0})   # +1.6% -> TP
    t2_shadow.update(T2 + timedelta(minutes=8), {LONG: 5_200.0})   # 이후 하락은 first_hit 를 바꾸지 않음
    r = t2_shadow.rows()[0]
    assert r["first_hit"] == t2_shadow.HIT_TP and r["done"] is False
    assert r["mfe_pct"] > 1.5 and r["mae_pct"] < -1.9
    t2_shadow.update(T2 + timedelta(minutes=31), {LONG: 5_300.0})
    assert t2_shadow.rows()[0]["done"] is True


def test_virtual_sl_first(world):
    _recorded(world)
    t2_shadow.update(T2 + timedelta(minutes=2), {LONG: 5_250.0})   # −1.04%
    assert t2_shadow.rows()[0]["first_hit"] == t2_shadow.HIT_SL


def test_virtual_none_after_30m(world):
    _recorded(world)
    t2_shadow.update(T2 + timedelta(minutes=10), {LONG: 5_320.0})
    t2_shadow.update(T2 + timedelta(minutes=30), {LONG: 5_320.0})
    r = t2_shadow.rows()[0]
    assert r["first_hit"] == t2_shadow.HIT_NONE and r["done"] is True


# ── 집계 ────────────────────────────────────────────────────────────────────

def test_summary_counts():
    rows = [
        dict(trading_date="20261026", T2_SHADOW="PASS", T2_vs_T3_DELTA_BP=20.0, T2_WHIPSAW=False, first_hit="TP_+1.5"),
        dict(trading_date="20261026", T2_SHADOW="PASS", T2_vs_T3_DELTA_BP=-5.0, T2_WHIPSAW=False, first_hit="SL_-1.0"),
        dict(trading_date="20261027", T2_SHADOW="PASS", T2_vs_T3_DELTA_BP=None, T2_WHIPSAW=True, first_hit="SL_-1.0"),
        dict(trading_date="20261027", T2_SHADOW="FAIL", T2_vs_T3_DELTA_BP=None, T2_WHIPSAW=None, first_hit=""),
    ]
    s = t2_shadow.summary(rows)
    assert (s["flags"], s["pass"], s["t3_filled"], s["t2_better"], s["t2_worse"]) == (4, 3, 2, 1, 1)
    assert s["avg_delta_bp"] == 7.5 and s["whipsaw"] == 1 and s["whipsaw_sl"] == 1
    assert s["virtual_total_pct"] == -0.5 and s["virtual_pf"] == 0.75 and s["virtual_winrate"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["by_day_pct"] == {"20261026": 0.5, "20261027": -1.0} and s["virtual_mdd_pct"] == -1.0
    assert s["ready_for_review"] is False


# ── 주문 경로 무영향 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("on", [False, True])
def test_run_once_orders_identical_with_and_without_t2_shadow(monkeypatch, tmp_path, on):
    """같은 시나리오(3-SLOT T+3 정상 체결)를 ON/OFF 각각 **별도 테스트**로 돌려 같은 결과인지 본다
    (한 테스트에서 run_once 두 번은 원장·claim 이 남아 두 번째가 조용히 비므로 쓰지 않는다)."""
    from tests.macd2.test_p3_retry_postentry import _setup
    from tests.macd2.test_tw2_3slot_worker_regression import _PRIOR_DAY, _prime_3slot_pending
    monkeypatch.setattr(config, "T2_SHADOW_ENABLED", on)
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert [(o.side, o.symbol, o.requested_qty, o.executed_price) for o in broker.orders] == [("BUY", LONG, 556, 15_010.0)]
    assert list(r.actions) == [f"TW2_3SLOT_ENTRY:{LONG}"]
    assert state.position.quantity == 556 and state.tw2_3slot_slots_used_today == 1


# ── UI (표시 전용) ──────────────────────────────────────────────────────────

def test_ui_shows_t2_shadow_expander_only_with_mu_assist(monkeypatch):
    from tests.macd2.test_mu_assist_xs import _ui
    t2_shadow._save({"20261026_123300_UP_RED": {
        "flag_id": "20261026_123300_UP_RED", "trading_date": "20261026", "flag_bar_at": F.isoformat(),
        "direction": "UP_RED", "T2_SHADOW": "PASS", "t2_at": T2.isoformat(), "T2_ENTRY_PRICE": 5305.0,
        "T3_ENTRY_PRICE": 5330.0, "T2_vs_T3_DELTA_BP": 47.13, "T2_WHIPSAW": False, "first_hit": "TP_+1.5",
        "done": True}})
    at = _ui(True, True, monkeypatch)
    labels = [e.label for e in at.expander]
    assert any(l.startswith("T2 SHADOW (기록 전용 · 주문 무관) — PASS 1건") for l in labels), labels
    at = _ui(False, False, monkeypatch)
    assert not any(str(e.label).startswith("T2 SHADOW") for e in at.expander)
