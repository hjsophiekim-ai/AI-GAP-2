"""P3 성능 smoke — Render worker 가 P3 때문에 무거워지지 않는가 (2026-09-27).

절대 수치를 고정하지 않는다(머신마다 다르다). **구조적 성질**과 N1 대비
상대치만 본다:

  · 런타임에 80일 replay 를 돌리지 않는다
  · 섀도우가 지표를 따로 계산하지 않고 worker 가 만든 ``bars_3m`` 을 받아 쓴다
  · tick 마다 ledger 전체를 쓰지 않는다(완료거래가 생길 때만 쓴다)
  · UI 렌더가 섀도우 시뮬레이션을 다시 돌리지 않는다
"""
from __future__ import annotations

import inspect
import time
from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    shadow_base,
    state_store,
    strategy_mode,
    worker,
)
from app.trading.macd2.worker import run_once
from tests.macd2.test_early_take_profit_worker import _broker, _market
from tests.macd2.test_p3_worker import _isolate_shadow_ledger, _ledger_before, _p3_state
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

KST = config.KST


# ── 구조적 성질 ──────────────────────────────────────────────────────────
def test_worker_never_replays_history_at_runtime():
    """런타임 경로에 80일 replay / build_ctx 류가 없어야 한다."""
    src = inspect.getsource(worker)
    for needle in ("build_ctx", "run_chain", "hengine", "A_BASE", "replay_80"):
        assert needle not in src, f"worker 가 런타임에 {needle} 를 부른다"


def test_shadow_receives_indicators_instead_of_recomputing_them():
    """섀도우 진입점은 전부 ``bars_3m`` 을 **인자로** 받는다 -- 따로 만들지 않는다."""
    for fn in (shadow_base.advance_exits, shadow_base.on_confirmed_flag,
               shadow_base.advance_c1, shadow_base.advance_whipsaw_watch,
               shadow_base.advance_h50_release):
        assert "bars_3m" in inspect.signature(fn).parameters, fn.__name__
    src = inspect.getsource(shadow_base)
    for needle in ("resample_completed_3m", "calculate_macd(", "get_history_df"):
        assert needle not in src, f"shadow_base 가 {needle} 로 지표를 다시 만든다"


def test_ledger_is_written_only_when_a_shadow_trade_completes():
    """tick 마다 쓰지 않는다 -- ``append_trade`` 호출부는 완료 경로 하나뿐이다."""
    src = inspect.getsource(shadow_base)
    assert src.count("chop_regime.append_trade(") == 1
    # 그 유일한 호출부는 _close (완료거래 기록) 안에 있다.
    close_src = inspect.getsource(shadow_base._close)
    assert "chop_regime.append_trade(" in close_src


def test_ui_helpers_do_not_run_the_shadow_simulation():
    """UI 가 읽는 함수들은 state/ledger 만 본다 -- 시뮬레이션을 다시 돌리지 않는다."""
    for fn in (strategy_mode.execution_layer, strategy_mode.shadow_status,
               strategy_mode.account_kind, strategy_mode.current):
        src = inspect.getsource(fn)
        for needle in ("advance_exits", "on_confirmed_flag", "run_once", "advance_c1"):
            assert needle not in src, f"{fn.__name__} 이 {needle} 를 부른다"


# ── N1 vs P3 상대 비용 ───────────────────────────────────────────────────
def _tick_loop(monkeypatch, tmp_path, mode: str, n: int = 12):
    _isolate_shadow_ledger(monkeypatch, tmp_path / mode)
    svc, now0 = _market(inverse_price=10_020.0)
    if mode == strategy_mode.MODE_P3:
        chop_regime.save_ledger(_ledger_before(now0, h50=5, tp1=0), source="perf")
    state = _p3_state(now=now0, mode=mode, entry_regime=None, bar_close=10_020.0)
    state.mode = "mock"
    broker = _broker(10_020.0)
    _patch_common(monkeypatch)

    writes = {"n": 0}
    _orig = chop_regime.save_ledger

    def _counting(trades, *, source="worker"):
        writes["n"] += 1
        return _orig(trades, source=source)

    monkeypatch.setattr(chop_regime, "save_ledger", _counting)

    samples = []
    for i in range(n):
        t = time.perf_counter()
        run_once(broker=broker, state=state, market_data=svc,
                 now=now0 + timedelta(seconds=5 * i))
        samples.append(time.perf_counter() - t)
    samples.sort()
    return {
        "avg": sum(samples) / len(samples),
        "p95": samples[max(0, int(len(samples) * 0.95) - 1)],
        "ledger_writes": writes["n"],
    }


def test_p3_does_not_blow_up_tick_latency(monkeypatch, tmp_path):
    """P3 tick 이 N1 대비 과도하게 무거워지지 않아야 한다.

    절대치가 아니라 배수로 본다. 상한은 넉넉히 잡는다 -- 이 테스트가 잡으려는
    것은 '한 자릿수 배수'가 아니라 '런타임 replay 같은 구조적 폭발'이다.
    """
    n1 = _tick_loop(monkeypatch, tmp_path, strategy_mode.MODE_N1)
    p3 = _tick_loop(monkeypatch, tmp_path, strategy_mode.MODE_P3)

    print(f"\nN1 avg={n1['avg']*1000:.1f}ms p95={n1['p95']*1000:.1f}ms "
          f"ledger_writes={n1['ledger_writes']}")
    print(f"P3 avg={p3['avg']*1000:.1f}ms p95={p3['p95']*1000:.1f}ms "
          f"ledger_writes={p3['ledger_writes']}")

    assert n1["ledger_writes"] == 0, "N1 모드에서 섀도우 ledger 를 쓰면 안 된다"
    # 완료거래가 없는 구간이면 P3 도 ledger 를 쓰지 않는다.
    assert p3["ledger_writes"] <= 1, (
        f"tick 마다 ledger 를 쓰고 있다: {p3['ledger_writes']}회")

    ratio = p3["avg"] / max(n1["avg"], 1e-9)
    assert ratio < 8.0, f"P3 tick 이 N1 대비 {ratio:.1f}배 -- 구조적 폭발 의심"
