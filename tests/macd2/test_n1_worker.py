"""N1 — worker 경로 / 실거래 안전성 테스트 (2026-09-20).

고정하는 계약
-------------
A. N1 OFF parity — N1 이 아니면 n1_adaptive 모듈 함수가 **단 한 번도** 호출되지
   않고, 오전 래더에 넘어가는 인자가 기존과 완전히 같다.
B. override 주입 — N1 이면 TP1/TP1비중/TP2 가 adaptive 판정값으로 넘어간다.
C. 완성봉 멱등 — 같은 완성봉을 두 번 평가하지 않는다(n1_last_eval_bar_ts).
D. 주문 경로   — N1 은 새 청산사유를 만들지 않고 기존 order_executor 경로를
   쓴다(worker 소스에 N1 전용 broker 호출이 없다).
E. 안전성      — 활성 전략은 항상 1개 이하, N1/C1 state 일관성,
   지연 재생(late completed bar replay)에서 N1 이 주문을 내지 않는다.
"""
from __future__ import annotations

import inspect

import app.trading.macd2.worker as wk
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import (
    config, n1_adaptive as na, peak_protection as pp, time_window_3slot as tw3, worker,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState

KST = config.KST
NOW = datetime(2026, 9, 18, 10, 33, tzinfo=KST)
BAR = datetime(2026, 9, 18, 10, 30, tzinfo=KST)
WORKER_SRC = Path(worker.__file__).read_text(encoding="utf-8")


class _Snap:
    def __init__(self, bar_dt=BAR):
        self.bar_dt = bar_dt
        self.hist = -1.0


def _pos(symbol=None, qty=100, avg=10_000.0):
    return PositionSnapshot(symbol=symbol or config.LONG_SYMBOL, quantity=qty, avg_price=avg)


def _state(**kw) -> RuntimeState:
    s = RuntimeState()
    s.time_window_position_active = True
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _bars(closes, start=None):
    start = start or datetime(2026, 9, 18, 9, 0, tzinfo=KST)
    return pd.DataFrame({
        "datetime": [start + timedelta(minutes=3 * i) for i in range(len(closes))],
        "open": closes, "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes], "close": closes,
        "volume": [1000.0] * len(closes),
    })


_RISING = [10_000.0 * (1.003 ** i) for i in range(80)]


# ── A. OFF parity ────────────────────────────────────────────────────────
@pytest.mark.parametrize("why, kw", [
    ("N1 토글 OFF", {}),
    ("H50", dict(time_window_h50_filter_enabled=True)),
    ("X2-lite", dict(time_window_x2lite_filter_enabled=True)),
    ("TW2 3-SLOT", dict(time_window_3slot_filter_enabled=True)),
])
def test_off_never_touches_n1_module(monkeypatch, why, kw):
    for name in ("snapshot", "resolve_ladder", "note_eval", "trend_ladder",
                 "off_trend_ladder", "cached_ladder"):
        monkeypatch.setattr(
            na, name,
            lambda *a, _n=name, **k: pytest.fail(f"N1 아닌데 n1_adaptive.{_n} 호출됨"))
    s = _state(**kw)
    assert worker._n1_ladder_overrides(s) == {}, why
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(_RISING),
                                position=_pos())
    assert s.n1_last_eval_bar_ts is None, why
    assert s.n1_effective_tp2 is None, why


def test_kill_switch_off_is_a_full_noop(monkeypatch):
    monkeypatch.setattr(config, "N1_ENABLED", False)
    monkeypatch.setattr(na, "resolve_ladder",
                        lambda *a, **k: pytest.fail("kill switch OFF 인데 판정됨"))
    s = _state(time_window_n1_filter_enabled=True)
    assert worker._n1_ladder_overrides(s) == {}
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(_RISING),
                                position=_pos())
    assert s.n1_last_eval_bar_ts is None


# ── B. override 주입 ─────────────────────────────────────────────────────
def test_trend_ladder_is_injected():
    s = _state(time_window_n1_filter_enabled=True)
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(_RISING),
                                position=_pos())
    assert s.n1_regime_state == na.REGIME_TREND
    ov = worker._n1_ladder_overrides(s)
    assert ov == {"tp2_pct_override": pytest.approx(8.0),
                  "tp1_sell_ratio_override": pytest.approx(0.0),
                  "tp1_pct_override": pytest.approx(3.5)}


def test_off_trend_ladder_is_injected():
    closes = _RISING[:70] + [_RISING[69] * 0.90] * 6
    s = _state(time_window_n1_filter_enabled=True)
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(closes),
                                position=_pos())
    assert s.n1_regime_state == na.REGIME_OFF_TREND
    ov = worker._n1_ladder_overrides(s)
    assert ov == {"tp2_pct_override": pytest.approx(4.0),
                  "tp1_sell_ratio_override": pytest.approx(0.2),
                  "tp1_pct_override": pytest.approx(3.0)}


def test_no_cache_falls_back_to_off_trend():
    """판정 전(포지션 첫 틱)에는 보수적으로 비추세 래더를 쓴다."""
    s = _state(time_window_n1_filter_enabled=True)
    assert na.cached_ladder(s) is None
    assert worker._n1_ladder_overrides(s)["tp2_pct_override"] == pytest.approx(4.0)


def test_flat_position_is_ignored():
    s = _state(time_window_n1_filter_enabled=True, time_window_position_active=False)
    assert worker._n1_ladder_overrides(s) == {}
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(_RISING),
                                position=_pos())
    assert s.n1_last_eval_bar_ts is None


def test_position_gone_clears_cache():
    s = _state(time_window_n1_filter_enabled=True, n1_regime_state=na.REGIME_TREND,
               n1_effective_tp2=8.0, n1_last_eval_bar_ts=BAR.isoformat())
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(), bars_3m=_bars(_RISING),
                                position=None)
    assert s.n1_regime_state is None and s.n1_effective_tp2 is None


# ── C. 완성봉 멱등 ───────────────────────────────────────────────────────
def test_same_bar_is_not_re_evaluated(monkeypatch):
    s = _state(time_window_n1_filter_enabled=True)
    calls = []
    real = na.resolve_ladder
    monkeypatch.setattr(na, "resolve_ladder",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    for _ in range(4):
        worker._advance_n1_adaptive(state=s, macd_snap=_Snap(BAR),
                                    bars_3m=_bars(_RISING), position=_pos())
    assert len(calls) == 1, "같은 완성봉을 여러 번 판정했다"
    worker._advance_n1_adaptive(state=s, macd_snap=_Snap(BAR + timedelta(minutes=3)),
                                bars_3m=_bars(_RISING), position=_pos())
    assert len(calls) == 2


def test_missing_snap_is_a_noop():
    s = _state(time_window_n1_filter_enabled=True)
    worker._advance_n1_adaptive(state=s, macd_snap=None, bars_3m=_bars(_RISING),
                                position=_pos())
    assert s.n1_last_eval_bar_ts is None


# ── D. 주문 경로 / 청산사유 ──────────────────────────────────────────────
def test_n1_creates_no_new_exit_reason():
    """N1 은 기존 사유를 그대로 쓴다(N1_SPEC.md §4) — 새 사유 상수를 만들지 않았다."""
    src = Path(na.__file__).read_text(encoding="utf-8")
    assert "EXIT_" not in src, "n1_adaptive 가 청산사유를 만들면 안 된다"
    for bogus in ("N1_TP2_FULL", "N1_OFF_TP2"):
        assert not hasattr(config, bogus), bogus
        assert bogus not in WORKER_SRC


def test_n1_module_never_calls_broker():
    src = Path(na.__file__).read_text(encoding="utf-8")
    for token in ("broker", "order_executor", "execute_exit", "requests", "http"):
        assert token not in src, token


def test_adaptive_update_runs_before_the_priority_chain():
    """판정 갱신은 보유 포지션 우선순위 체인 **시작부**에서 돌고, 주문을
    내지 않는다(다음 tick 의 래더가 그 값을 읽는다)."""
    i = WORKER_SRC.index("_advance_n1_adaptive(state=state")
    j = WORKER_SRC.index("tw_resolve_outcome = _resolve_time_window_candidate(")
    assert i < j
    seg = WORKER_SRC[i - 400:i + 600]
    assert "except Exception" in seg, "판정 실패가 tick 을 죽이면 안 된다"


def test_ladder_overrides_are_passed_to_both_paths():
    assert "tp1_pct_override=_n1_over.get(\"tp1_pct_override\")" in WORKER_SRC
    assert WORKER_SRC.count("_n1_over = _n1_ladder_overrides(state)") == 1
    # 틱 TP 경로와 완성봉 래더 경로 두 곳 모두에서 쓰인다
    assert WORKER_SRC.count("_n1_over.get(") >= 3
    assert "_pm_tp2 = _n1_over[\"tp2_pct_override\"]" in WORKER_SRC


def test_quality_threshold_override_is_mode_driven():
    assert ("quality_threshold_override=time_window_3slot.quality_score_threshold("
            in WORKER_SRC)


# ── E. 실거래 안전성 ─────────────────────────────────────────────────────
def test_at_most_one_strategy_can_be_active():
    """모드 판정은 결정론적으로 하나만 돌려준다 — 토글이 여러 개 켜져 있어도."""
    s = _state(time_window_n1_filter_enabled=True, time_window_h50_filter_enabled=True,
               time_window_x2lite_filter_enabled=True,
               time_window_3slot_filter_enabled=True)
    mode = tw3.active_3slot_mode(s)
    assert mode == tw3.MODE_TW2_3SLOT          # 기존 우선순위 보존(N1 은 마지막)
    assert na.is_active(s) is False            # N1 이 이기지 않는다
    assert pp.is_active(s) is False


def test_n1_is_last_in_mode_priority():
    flags = [f for f, _ in tw3._MODE_BY_FLAG]
    assert flags[-1] == "time_window_n1_filter_enabled"
    assert flags[:-1] == ["time_window_3slot_filter_enabled",
                          "time_window_twf_filter_enabled",
                          "time_window_x2lite_filter_enabled",
                          "time_window_h50_filter_enabled"]


def test_c1_requires_n1():
    n1 = _state(time_window_n1_filter_enabled=True, c1_peak_protection_enabled=True)
    assert pp.is_active(n1) is True
    for other in ("time_window_h50_filter_enabled", "time_window_x2lite_filter_enabled",
                  "time_window_3slot_filter_enabled"):
        s = _state(c1_peak_protection_enabled=True, **{other: True})
        assert pp.is_active(s) is False, other


def test_scheduled_entry_is_blocked_in_n1():
    assert tw3.scheduled_entry_supported(_state(time_window_n1_filter_enabled=True)) is False


def test_late_completed_bar_replay_path_has_no_n1_orders():
    """지연 재생 블록(청산 tick 의 플래그 보존 경로)은 기록 전용이다 —
    그 안에서 N1/C1 판정이나 주문이 일어나지 않는다."""
    i = WORKER_SRC.index("_propagate_confirmed_flag_without_orders")
    seg = WORKER_SRC[max(0, i - 3000):i + 500]
    assert "_advance_n1_adaptive" not in seg
    assert "_advance_c1_peak_protection" not in seg


def test_reset_sites_clear_both_n1_and_c1():
    """포지션 수명 리셋은 **중앙 함수 한 곳**에서만 일어난다 (2026-09-21).

    원래 이 테스트는 "리셋 지점 개수가 서로 같다"로 N1/C1 동반 정리를 확인했다.
    2026-09-21 실사고(수동매도 후 stale H50 이 새 포지션을 즉시 청산)의 원인이
    바로 그 '지점마다 개별 나열' 구조였다 — C1/N1 은 나열됐는데 H50/whipsaw-watch
    만 일부 경로에서 빠졌다. 그래서 등록 지점을 하나로 모았고, 이 테스트도
    같은 의도를 **더 강한 불변식**으로 검증한다.
    """
    body = inspect.getsource(wk._clear_position_scoped_state)
    # 중앙 함수가 네 가지 position-scoped 상태를 전부 정리한다
    for call in ("n1_adaptive.clear(state)", "peak_protection.clear(state)",
                 "small_whipsaw_hold.clear(state)", "_clear_whipsaw_watch(state)"):
        assert call in body, call
    # 소유권 키도 함께 끊는다
    assert "state.h50_owner_epoch = 0" in body
    assert "state.c1_owner_epoch = 0" in body

    # 중앙 함수 밖에 남아도 되는 clear 는 '일자 rollover' 와 '각 모듈이 자기
    # 진행 루틴 안에서 스스로 끝내는' 경우뿐이다. 포지션 lifecycle 전이에서는
    # 어떤 모듈도 개별적으로 clear 되지 않는다.
    assert WORKER_SRC.count("n1_adaptive.clear(state)") == 3        # 중앙 + 일자변경 + N1 자체
    # 중앙 + 일자변경 + C1 자체진행(포지션없음) + C1 stale-owner 폐기
    assert WORKER_SRC.count("peak_protection.clear(state)") == 4
    # H50 은 _advance_h50_hold 안에서 자기정리 가드가 많다:
    #   중앙 + 일자변경 + (포지션없음 / stale-epoch / stale-started_at / held_dir없음) + 해제후
    assert WORKER_SRC.count("small_whipsaw_hold.clear(state)") == 7


def test_every_position_lifecycle_transition_uses_the_central_clearer():
    """진입/청산/플랫복구 전이는 전부 중앙 함수를 통과한다 (2026-09-21)."""
    # 신규 포지션 시작 (3-SLOT / TW2 / 프리마켓 / reconcile 입양)
    assert WORKER_SRC.count("_begin_position_epoch(state, reason=") == 4
    # 전량청산 · 스위치 매도레그 flat · RECOVERED_TO_FLAT
    assert WORKER_SRC.count('_clear_position_scoped_state(state, reason="FULL_EXIT")') == 1
    assert WORKER_SRC.count('_clear_position_scoped_state(state, reason="SWITCH_SELL_LEG_FLAT")') == 1
    assert WORKER_SRC.count("_clear_position_scoped_state(state, reason=RECOVERED_TO_FLAT)") == 1


def test_n1_adaptive_is_pure_except_note_helpers():
    src = inspect.getsource(na)
    # state 를 쓰는 함수는 note_eval / clear 뿐
    writers = re.findall(r"state\.\w+ =", src)
    assert writers, "note 헬퍼가 있어야 한다"
    for fn in ("snapshot", "resolve_ladder", "trend_ladder", "off_trend_ladder",
               "thresholds_etp", "cached_ladder"):
        body = inspect.getsource(getattr(na, fn))
        assert "state." not in body or fn == "cached_ladder", fn
