"""AR1 — 오후 동일방향 재진입 예외 (2026-09-24 내장, 토글 없음).

원칙
----
* 적용대상은 ``REJECT_SAME_DIRECTION_AFTERNOON`` 거절 **하나뿐**이다(PLB 금지).
* TEG 탈락이 ``price_ema_stack_aligned`` 하나뿐이고 momentum 3조건이 전부 참일
  때만 그 조건 하나를 면제한다. 새 numerical threshold 를 만들지 않는다.
* 통과는 "즉시 주문"이 아니다 — 동일방향 거절만 풀고 기존 파이프라인을 그대로 탄다.
* X1 CONTEXT 토글/state/모듈은 production 에서 완전히 사라졌다.

주의: 이 디렉토리에서는 ``monkeypatch.undo()`` 를 쓰지 않는다.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass

import pandas as pd
import pytest

from app.trading.macd2 import config, state_store, teg_gate as T, worker
from app.trading.macd2 import time_window_3slot as tw3

KST = "Asia/Seoul"
SAME_DIR = config.TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND


@dataclass
class FakeTeg:
    approved: bool
    conditions: dict
    metrics: dict = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


def teg_conditions(**overrides):
    base = {c: True for c in T.ALL_CONDITIONS}
    base.update(overrides)
    return base


# ── 1. 예외 판정 자체 ──────────────────────────────────────────────────────
def test_ar1_grants_stack_exemption_when_only_stack_fails():
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False}))
    d = tw3.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is True and d.stack_exempt is True
    assert d.reason == tw3.AR1_ALLOW_REASON


def test_ar1_refuses_when_two_conditions_fail():
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False, T.COND_VWAP: False}))
    d = tw3.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is False and d.reason == tw3.AR1_REJECT_MULTI_FAIL


@pytest.mark.parametrize("off", [T.COND_MACD_GAP_EXPANDING, T.COND_EMA_SPREAD_EXPANDING,
                                 T.COND_VWAP])
def test_ar1_refuses_when_momentum_not_confirmed(off):
    """momentum 3조건 중 하나라도 꺼지면 거부한다(탈락 2개 -> MULTI 에서 먼저 걸린다)."""
    conds = teg_conditions(**{T.COND_EMA_STACK: False})
    conds[off] = False
    d = tw3.evaluate_afternoon_reentry(FakeTeg(False, conds), base_reject_reason=SAME_DIR)
    assert d.allowed is False
    assert d.reason in (tw3.AR1_REJECT_MULTI_FAIL, tw3.AR1_REJECT_NO_MOMENTUM)


def test_ar1_is_narrow_no_plb():
    """SAME_DIRECTION_AFTERNOON 이 아닌 거절에는 절대 개입하지 않는다."""
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False}))
    for other in ("TW2_3SLOT_REJECT_TEG", "REJECT_LOW_QUALITY_SCORE",
                  config.TW2_3SLOT_REJECT_SLOT_CAP,
                  config.TW2_3SLOT_REJECT_OUTSIDE_WINDOW, None):
        d = tw3.evaluate_afternoon_reentry(teg, base_reject_reason=other)
        assert d.allowed is False and d.reason == tw3.AR1_REJECT_OUT_OF_SCOPE


def test_ar1_without_teg_conditions_is_refused():
    d = tw3.evaluate_afternoon_reentry(FakeTeg(False, {}), base_reject_reason=SAME_DIR)
    assert d.allowed is False and d.reason == tw3.AR1_REJECT_NO_CONDITIONS


def test_ar1_passes_through_already_approved_teg_without_exemption():
    teg = FakeTeg(True, teg_conditions())
    d = tw3.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is True and d.stack_exempt is False
    assert d.reason == tw3.AR1_ALLOW_TEG_APPROVED


def test_ar1_is_pure_no_new_thresholds():
    """AR1 은 TEG 조건 boolean 만 읽는다 — 자체 임계값이 없다."""
    src = inspect.getsource(tw3.evaluate_afternoon_reentry)
    assert "config." not in src


# ── 2. 과거 앵커 — 2026-09-22 12:15 DOWN_BLUE ──────────────────────────────
def test_anchor_20260922_1215_blue_is_ar1_reentry():
    """실측(z4_teg): TEG 7조건 중 price_ema_stack_aligned **하나만** 실패
    (close 1,917,000 / ema10 1,917,384 / ema20 1,917,267 = 117원 차이),
    macd_gap 2봉 +179.32 / ema_spread +247.39 / vwap 우호는 전부 참.
    threshold 튜닝 금지 — 행동 확인용 fixture 다."""
    conds = teg_conditions(**{T.COND_EMA_STACK: False})
    metrics = {"close": 1917000.0, "ema10": 1917384.0097166637,
               "ema20": 1917267.1617275702, "vwap": 1922601.7482286782,
               "gap_flag": 66.6995748199916, "gap_now": 96.14664951972088}
    d = tw3.evaluate_afternoon_reentry(FakeTeg(False, conds, metrics),
                                       base_reject_reason=SAME_DIR)
    assert d.allowed is True and d.stack_exempt is True
    assert d.metrics["close"] == metrics["close"]


# ── 3. resolve_slot 이 실제로 그 사유로 거절하는가 ─────────────────────────
def test_resolve_slot_still_rejects_same_direction_afternoon():
    sd = tw3.resolve_slot(
        now=pd.Timestamp("2026-09-22 12:15", tz=KST).to_pydatetime(),
        slots_used_today=1, morning_count=0, afternoon_count=1,
        direction="DOWN_BLUE", is_flat=True, last_afternoon_direction="DOWN_BLUE",
    )
    assert sd.slot_allowed is False
    assert sd.reject_reason == tw3.REJECT_SAME_DIRECTION_AFTERNOON


# ── 4. worker 배선 ─────────────────────────────────────────────────────────
def _worker_src() -> str:
    return inspect.getsource(worker)


def test_worker_ar1_hook_is_scoped_and_untoggled():
    src = _worker_src()
    start = src.index("# ── AR1: 오후 동일방향 재진입 예외")
    body = src[start:start + 2200]
    assert "time_window_3slot.REJECT_SAME_DIRECTION_AFTERNOON" in body
    assert "time_window_3slot.evaluate_afternoon_reentry" in body
    for forbidden in ("X1_ACT_AR1", "_x1_enabled", "x1_context_enabled"):
        assert forbidden not in body


def test_worker_ar1_does_not_short_circuit_the_pipeline():
    """AR1 은 approve 플래그만 세운다 — 여기서 직접 주문/사이징을 하지 않는다."""
    src = _worker_src()
    start = src.index("# ── AR1: 오후 동일방향 재진입 예외")
    body = src[start:start + 2200]
    for forbidden in ("broker.", "place_order", "smart_sizing", "position_sizing"):
        assert forbidden not in body


# ── 5. X1 잔재가 production 에 없다 ────────────────────────────────────────
def test_worker_has_no_x1_wiring_left():
    src = _worker_src()
    for forbidden in ("x1_context", "x1_shadow", "_advance_x1_flip_exit",
                      "_x1_reset_position_state", "_x1_record_live_flag",
                      "EXIT_X1_FLIP_EXIT"):
        assert forbidden not in src, forbidden


def test_x1_modules_are_gone_from_production_package():
    for mod in ("app.trading.macd2.x1_context", "app.trading.macd2.x1_shadow"):
        with pytest.raises(ImportError):
            __import__(mod)


def test_x1_config_constants_are_gone():
    for name in ("X1_ENABLED", "X1_ACT_AR1", "X1_ACT_FLIP_EXIT", "X1_AR1_ENABLED",
                 "X1_FILTER_VERSION", "EXIT_X1_FLIP_EXIT", "X1_FLAG_HISTORY_MAX"):
        assert not hasattr(config, name), name


def test_runtime_state_has_no_x1_fields():
    s = state_store.default_state()
    assert not [f for f in s.__dataclass_fields__ if f.startswith("x1_")]
    assert not hasattr(s, "last_x1_trace")


def test_service_has_no_x1_command():
    from app.trading.macd2 import service as svc
    assert not [n for n in dir(svc.Macd2Service) if "x1" in n.lower()]


def test_old_state_with_x1_keys_still_loads():
    """구버전 state 파일(x1_* 키 포함)을 읽어도 깨지지 않는다."""
    raw = state_store.serialize(state_store.default_state())
    raw.update({"x1_context_enabled": True, "x1_shadow_mode_enabled": True,
                "x1_flag_history": [["2026-09-22T09:00:00+09:00", "UP_RED"]],
                "x1_flip_exit_armed": True, "last_x1_trace": {"a": 1}})
    s = state_store.deserialize(raw)
    assert not hasattr(s, "x1_context_enabled")


def test_serialized_state_has_no_x1_keys():
    raw = state_store.serialize(state_store.default_state())
    assert not [k for k in raw if "x1" in k.lower()]
