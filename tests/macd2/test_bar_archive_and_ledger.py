"""bar_archive / bar_ledger — 관측 전용 기능 테스트 (2026-09-08).

A. 정적 회귀 — 거래 로직 함수의 AST 가 관측 훅 추가 외에는 그대로인가
B. archive  — 최초 저장 / revision 증가 / load_frame_as_of 시점복원 / 재시작 경계
C. bar ledger — 중복기록 없음 / production 계산값과 동일 / write 실패해도 거래 지속
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import bar_archive, bar_ledger, config, worker
from app.trading.macd2.models import Direction

KST = config.KST


def _frame(rows):
    return pd.DataFrame(
        [{"datetime": pd.Timestamp(t, tz=KST), "open": o, "high": h,
          "low": lo, "close": c, "volume": v} for t, o, h, lo, c, v in rows]
    )


# ══════════════════════════════════════════════════════════════════════════
# A. 정적 회귀 — 거래 로직 변경 0
# ══════════════════════════════════════════════════════════════════════════

def test_advance_confirmed_primary_decision_logic_unchanged():
    """크로스 판정 본문이 관측 코드 말고는 그대로여야 한다.

    관측으로 추가된 것은 (1) 직전 상태를 읽는 대입 1개 (2) try/except 로 감싼
    bar_ledger 호출 1개뿐이다. 그 둘을 걷어내면 판정 흐름은 이전과 동일한
    문장 시퀀스여야 한다.
    """
    src = textwrap.dedent(inspect.getsource(worker._advance_confirmed_primary))
    fn = ast.parse(src).body[0]
    body = [n for n in fn.body if not isinstance(n, ast.Expr)
            or not isinstance(getattr(n, "value", None), ast.Constant)]

    # 관측 코드 제거
    stripped = []
    for node in body:
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", "") == "_observed_prev_direction":
            continue
        if isinstance(node, ast.Try):
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            names = {getattr(getattr(c.func, "value", None), "id", "") for c in calls}
            if "bar_ledger" in names:
                continue  # 관측 전용 블록
        stripped.append(node)

    kinds = [type(n).__name__ for n in stripped]
    assert kinds == ["Assign", "If", "Assign", "Assign", "Assign", "If",
                     "Assign", "If", "Return"], kinds

    # 판정 자체는 여전히 evaluate_macd_crossover(macd_snap, state.last_detected_direction)
    call = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "evaluate_macd_crossover"):
            call = node
    assert call is not None
    assert getattr(call.args[0], "id", "") == "macd_snap"
    assert getattr(call.args[1], "attr", "") == "last_detected_direction"


def test_observation_hooks_are_all_guarded():
    """관측 훅은 전부 try/except 안에 있어야 한다(거래 경로 보호)."""
    from app.trading.macd2 import market_data

    for mod in (worker, market_data):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            owner = getattr(getattr(node.func, "value", None), "id", "")
            fname = getattr(node.func, "attr", "")
            if owner not in ("bar_ledger", "bar_archive"):
                continue
            if fname in ("set_worker_instance_id", "record_bar", "record_frame"):
                # 해당 호출을 감싸는 Try 가 있는지 확인
                guarded = False
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.Try) and node in ast.walk(parent):
                        guarded = True
                        break
                assert guarded, f"{mod.__name__}: {owner}.{fname} not in try/except"


def test_bar_ledger_never_recomputes_macd():
    """bar_ledger 는 MACD/크로스를 절대 재계산하지 않는다 (호출 자체가 없다)."""
    tree = ast.parse(inspect.getsource(bar_ledger))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(getattr(node.func, "id", "") or getattr(node.func, "attr", ""))
    assert "calculate_macd" not in called
    assert "evaluate_macd_crossover" not in called
    # import 도 하지 않는다
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
    assert not ({"calculate_macd", "evaluate_macd_crossover"} & imported)


# ══════════════════════════════════════════════════════════════════════════
# B. archive
# ══════════════════════════════════════════════════════════════════════════

def test_archive_records_new_bars_once():
    now = datetime(2026, 9, 8, 8, 5, tzinfo=KST)
    df = _frame([("2026-09-08 08:00", 100, 101, 99, 100.5, 10),
                 ("2026-09-08 08:01", 100.5, 102, 100, 101, 12)])
    s1 = bar_archive.record_frame(df, now=now, source="bootstrap", worker_instance_id="w1")
    assert (s1["new"], s1["revised"], s1["errors"]) == (2, 0, 0)
    # 같은 프레임 재기록 -> 아무것도 append 되지 않는다
    s2 = bar_archive.record_frame(df, now=now + timedelta(minutes=1),
                                  source="incremental", worker_instance_id="w1")
    assert (s2["new"], s2["revised"], s2["unchanged"]) == (0, 0, 2)
    rows = bar_archive.load_day("20260908")
    assert len(rows) == 2
    assert set(rows["revision"]) == {0}
    assert set(rows["source"]) == {"bootstrap"}


def test_archive_revision_increments_on_value_change():
    t0 = datetime(2026, 9, 8, 8, 5, tzinfo=KST)
    bar_archive.record_frame(_frame([("2026-09-08 08:00", 100, 101, 99, 100.5, 10)]),
                             now=t0, source="bootstrap", worker_instance_id="w1")
    t1 = t0 + timedelta(minutes=3)
    s = bar_archive.record_frame(
        _frame([("2026-09-08 08:00", 100, 105, 99, 104.0, 33)]),
        now=t1, source="incremental", worker_instance_id="w1")
    assert (s["new"], s["revised"]) == (0, 1)
    rows = bar_archive.load_day("20260908").sort_values("revision")
    assert list(rows["revision"]) == [0, 1]
    # first_seen_at 은 최초 관측시각을 유지, last_seen_at 만 갱신
    assert rows.iloc[1]["first_seen_at"] == pd.Timestamp(t0)
    assert rows.iloc[1]["last_seen_at"] == pd.Timestamp(t1)
    assert float(rows.iloc[1]["close"]) == 104.0


def test_load_frame_as_of_restores_what_worker_saw():
    """T 시점에 존재하지 않던 봉은 빠지고, 그 시점까지의 정정만 반영된다."""
    t0 = datetime(2026, 9, 8, 8, 5, tzinfo=KST)
    bar_archive.record_frame(_frame([("2026-09-08 08:00", 100, 101, 99, 100.5, 10)]),
                             now=t0, source="bootstrap", worker_instance_id="w1")
    t1 = datetime(2026, 9, 8, 8, 8, tzinfo=KST)
    bar_archive.record_frame(
        _frame([("2026-09-08 08:00", 100, 101, 99, 100.5, 10),
                ("2026-09-08 08:03", 101, 103, 101, 102.0, 20)]),
        now=t1, source="incremental", worker_instance_id="w1")
    t2 = datetime(2026, 9, 8, 8, 20, tzinfo=KST)
    bar_archive.record_frame(   # 08:00 봉이 뒤늦게 정정됨
        _frame([("2026-09-08 08:00", 100, 101, 99, 100.9, 11),
                ("2026-09-08 08:03", 101, 103, 101, 102.0, 20)]),
        now=t2, source="incremental", worker_instance_id="w1")

    f0 = bar_archive.load_frame_as_of("20260908", t0)
    assert list(f0["datetime"].dt.strftime("%H:%M")) == ["08:00"]
    assert float(f0.iloc[0]["close"]) == 100.5      # 정정 전 값

    f1 = bar_archive.load_frame_as_of("20260908", t1)
    assert list(f1["datetime"].dt.strftime("%H:%M")) == ["08:00", "08:03"]
    assert float(f1.iloc[0]["close"]) == 100.5      # 아직 정정 전

    f2 = bar_archive.load_frame_as_of("20260908", t2)
    assert float(f2.iloc[0]["close"]) == 100.9      # 정정 반영

    # 아무것도 관측되지 않은 시점
    assert bar_archive.load_frame_as_of(
        "20260908", t0 - timedelta(hours=1)).empty


def test_archive_distinguishes_worker_restart():
    t0 = datetime(2026, 9, 8, 8, 5, tzinfo=KST)
    bar_archive.record_frame(_frame([("2026-09-08 08:00", 100, 101, 99, 100.5, 10)]),
                             now=t0, source="bootstrap", worker_instance_id="w1")
    # 재시작: 프로세스 내 인덱스가 사라져도 파일에서 복구해 revision 이 이어진다
    bar_archive._reset_for_tests()
    t1 = t0 + timedelta(minutes=5)
    s = bar_archive.record_frame(_frame([("2026-09-08 08:00", 100, 101, 99, 111.0, 99)]),
                                 now=t1, source="bootstrap", worker_instance_id="w2")
    assert (s["new"], s["revised"]) == (0, 1)
    rows = bar_archive.load_day("20260908").sort_values("revision")
    assert list(rows["worker_instance_id"]) == ["w1", "w2"]
    assert list(rows["revision"]) == [0, 1]


def test_archive_never_raises_on_bad_input():
    assert bar_archive.record_frame(None, now=datetime.now(KST))["errors"] == 0
    assert bar_archive.record_frame(pd.DataFrame(), now=datetime.now(KST))["errors"] == 0
    bad = pd.DataFrame([{"nope": 1}])
    assert bar_archive.record_frame(bad, now=datetime.now(KST))["errors"] == 0


def test_archive_has_no_module_level_instance_id():
    """instance_id 는 모듈 전역이면 안 된다 — 동시 Worker 가 서로 덮어쓴다."""
    assert not hasattr(bar_archive, "set_worker_instance_id")
    assert not hasattr(bar_archive, "_WORKER_INSTANCE_ID")


def test_market_data_carries_instance_id_per_instance():
    """MarketDataService 인스턴스마다 별도 instance_id 를 들고 있어야 한다."""
    from app.trading.macd2.market_data import MarketDataService

    a = MarketDataService(mode="mock")
    b = MarketDataService(mode="mock")
    assert a.observed_worker_instance_id == ""
    a.observed_worker_instance_id = "worker-A"
    b.observed_worker_instance_id = "worker-B"
    assert (a.observed_worker_instance_id, b.observed_worker_instance_id) == (
        "worker-A", "worker-B")


# ══════════════════════════════════════════════════════════════════════════
# 동시 Worker 안전성 (2026-09-03 dual-Worker 실사고 대비)
# ══════════════════════════════════════════════════════════════════════════

def test_observed_frame_is_thread_local():
    """모듈 전역이면 동시 Worker 가 서로의 프레임을 덮어쓴다."""
    import threading as _th

    assert isinstance(worker._OBSERVED_FRAME, _th.local)

    seen = {}
    barrier = _th.Barrier(2)

    def _run(name, marker):
        worker._set_observed_frame(marker, [name])
        barrier.wait(timeout=5)          # 상대 스레드가 자기 값을 심을 때까지 대기
        seen[name] = (getattr(worker._OBSERVED_FRAME, "bars_3m", None),
                      getattr(worker._OBSERVED_FRAME, "dropped_bar_starts", None))

    t1 = _th.Thread(target=_run, args=("A", "frame-A"))
    t2 = _th.Thread(target=_run, args=("B", "frame-B"))
    t1.start(); t2.start(); t1.join(5); t2.join(5)

    assert seen["A"] == ("frame-A", ["A"])
    assert seen["B"] == ("frame-B", ["B"])


def test_two_concurrent_workers_do_not_cross_contaminate_ledger_rows():
    """서로 다른 스레드의 두 Worker 가 각자 프레임 진단값으로 기록해야 한다."""
    import threading as _th

    done = _th.Barrier(2)
    results = {}

    def _tick(name, hhmm, premarket_times, wid):
        snap = _Snap(datetime(2026, 9, 8, 9, int(hhmm), tzinfo=KST),
                     1.0, 2.0, -1.0, 1.0)
        worker._set_observed_frame(_bars3m(premarket_times), [name])
        done.wait(timeout=5)             # 두 스레드 모두 심은 뒤에 기록
        bar_ledger.record_bar(macd_snap=snap, direction=Direction.HOLD,
                              prev_direction_state=None,
                              bars_3m=getattr(worker._OBSERVED_FRAME, "bars_3m", None),
                              dropped_bar_starts=getattr(
                                  worker._OBSERVED_FRAME, "dropped_bar_starts", None),
                              worker_instance_id=wid)
        results[name] = True

    t1 = _th.Thread(target=_tick, args=("A", 9, ["2026-09-08 08:27"], "w-A"))
    t2 = _th.Thread(target=_tick, args=("B", 12, ["2026-09-08 08:27",
                                                  "2026-09-08 08:30",
                                                  "2026-09-08 08:33"], "w-B"))
    t1.start(); t2.start(); t1.join(5); t2.join(5)

    df = bar_ledger.load("20260908").set_index(
        bar_ledger.load("20260908")["bar_at"].dt.strftime("%H:%M"))
    assert int(df.loc["09:09", "premarket_bars_in_frame"]) == 1     # A 의 프레임
    assert int(df.loc["09:12", "premarket_bars_in_frame"]) == 3     # B 의 프레임
    assert int(df.loc["09:09", "dropped_incomplete_bars"]) == 1
    assert df.loc["09:09", "worker_instance_id"] == "w-A"
    assert df.loc["09:12", "worker_instance_id"] == "w-B"


# ══════════════════════════════════════════════════════════════════════════
# C. bar ledger
# ══════════════════════════════════════════════════════════════════════════

class _Snap:
    def __init__(self, bar_dt, macd, signal, prev_diff, cur_diff, count=120):
        self.bar_dt = bar_dt
        self.macd = macd
        self.signal = signal
        self.hist = macd - signal
        self.hist_last3 = (prev_diff - 1, prev_diff, cur_diff)
        self.completed_3m_count = count
        self.previous_diff = prev_diff
        self.current_diff = cur_diff
        self.relation = "ABOVE"


def _bars3m(times):
    return pd.DataFrame({"datetime": [pd.Timestamp(t, tz=KST) for t in times]})


def test_bar_ledger_records_once_per_bar():
    snap = _Snap(datetime(2026, 9, 8, 9, 9, tzinfo=KST), 3530.0, 3391.6, -68.4, 138.3)
    bars = _bars3m(["2026-09-08 08:27", "2026-09-08 09:00", "2026-09-08 09:09"])
    assert bar_ledger.record_bar(macd_snap=snap, direction=Direction.UP_RED,
                                 prev_direction_state=Direction.DOWN_BLUE,
                                 bars_3m=bars, dropped_bar_starts=["x"],
                                 signal_id="sig-1", worker_instance_id="w1") is True
    # 같은 봉 재호출 -> 기록 안 함
    assert bar_ledger.record_bar(macd_snap=snap, direction=Direction.UP_RED,
                                 prev_direction_state=Direction.DOWN_BLUE,
                                 bars_3m=bars) is False
    df = bar_ledger.load("20260908")
    assert len(df) == 1
    r = df.iloc[0]
    assert r["direction"] == "UP_RED"
    assert bool(r["confirmed_cross"]) is True
    assert r["prev_direction_state"] == "DOWN_BLUE"
    assert float(r["macd"]) == 3530.0
    assert float(r["signal"]) == 3391.6
    assert float(r["gap"]) == 138.3
    assert float(r["prev_gap"]) == -68.4
    assert int(r["bars_in_frame"]) == 120
    assert int(r["premarket_bars_in_frame"]) == 1        # 08:27 만 장전
    assert int(r["dropped_incomplete_bars"]) == 1
    assert r["signal_id"] == "sig-1"


def test_bar_ledger_records_hold_bars_too():
    snap = _Snap(datetime(2026, 9, 8, 9, 12, tzinfo=KST), 5433.9, 3800.1, 138.3, 1633.8)
    assert bar_ledger.record_bar(macd_snap=snap, direction=Direction.HOLD,
                                 prev_direction_state=Direction.UP_RED,
                                 bars_3m=_bars3m(["2026-09-08 09:12"])) is True
    r = bar_ledger.load("20260908").iloc[0]
    assert r["direction"] == "HOLD"
    assert bool(r["confirmed_cross"]) is False
    assert str(r["signal_id"]) in ("", "nan")


def test_bar_ledger_gap_matches_crossover_fallback():
    """previous_diff/current_diff 가 None 이면 크로스 판정과 같은 fallback 을 쓴다."""
    snap = _Snap(datetime(2026, 9, 8, 10, 6, tzinfo=KST), 200.0, 150.0, 11.0, 22.0)
    snap.previous_diff = None
    snap.current_diff = None
    snap.hist_last3 = (1.0, -7.5, 50.0)
    bar_ledger.record_bar(macd_snap=snap, direction=Direction.DOWN_BLUE,
                          prev_direction_state=None, bars_3m=None)
    r = bar_ledger.load("20260908").iloc[0]
    assert float(r["prev_gap"]) == -7.5          # hist_last3[-2]
    assert float(r["gap"]) == 50.0               # macd - signal
    assert str(r["prev_direction_state"]) in ("", "nan")


def test_bar_ledger_write_failure_never_raises(monkeypatch):
    snap = _Snap(datetime(2026, 9, 8, 9, 9, tzinfo=KST), 1.0, 2.0, -1.0, 1.0)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(bar_ledger, "open", _boom, raising=False)
    monkeypatch.setattr("builtins.open", _boom)
    assert bar_ledger.record_bar(macd_snap=snap, direction=Direction.UP_RED,
                                 prev_direction_state=None) is False


def test_bar_ledger_refuses_production_path_outside_live_worker(monkeypatch):
    # 다른 테스트가 남긴 live-worker 마커에 영향받지 않도록 명시적으로 지운다
    # (그 마커가 이 프로세스 pid 면 가드가 정상적으로 통과시켜 버린다).
    monkeypatch.delenv(bar_ledger.LIVE_WORKER_MARKER_ENV, raising=False)
    monkeypatch.setattr(bar_ledger, "BAR_LEDGER_PATH",
                        bar_ledger._DEFAULT_BAR_LEDGER_PATH)
    bar_ledger._reset_for_tests()
    with pytest.raises(RuntimeError, match="REFUSING"):
        bar_ledger._assert_safe_to_write()


def test_bar_ledger_allows_production_path_for_the_live_worker(monkeypatch):
    import os as _os

    monkeypatch.setenv(bar_ledger.LIVE_WORKER_MARKER_ENV, str(_os.getpid()))
    monkeypatch.setattr(bar_ledger, "BAR_LEDGER_PATH",
                        bar_ledger._DEFAULT_BAR_LEDGER_PATH)
    bar_ledger._assert_safe_to_write()      # raise 하지 않아야 한다


def test_worker_keeps_trading_when_ledger_write_fails(monkeypatch):
    """record_bar 가 터져도 _advance_confirmed_primary 의 반환/상태는 그대로."""
    from app.trading.macd2.state_store import RuntimeState

    def _boom(**_k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(bar_ledger, "record_bar", _boom)
    state = RuntimeState()
    state.last_detected_direction = Direction.DOWN_BLUE
    now = datetime(2026, 9, 8, 9, 12, 5, tzinfo=KST)
    snap = _Snap(datetime(2026, 9, 8, 9, 9, tzinfo=KST), 3530.0, 3391.6, -68.4, 138.3)
    out = worker._advance_confirmed_primary(state, snap, now)
    assert out == Direction.UP_RED
    assert state.last_detected_direction == Direction.UP_RED
    assert state.latest_primary_flag == Direction.UP_RED
    assert state.latest_primary_signal_id
