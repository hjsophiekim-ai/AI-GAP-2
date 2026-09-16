"""늦게 완성된 완성봉 순차 평가 (2026-09-16 실사고).

사고
----
KIS 1분봉이 늦게 도착하면 그 순간 해당 3분봉은 filter_complete_3m_bars 에서
불완전으로 탈락하고(HISTORY_GAP), 뒤늦게 완성될 때쯤 프레임의 마지막 봉은
이미 다음 봉이다. ``_advance_confirmed_primary`` 는 **마지막 봉 하나만**
평가하므로 그 봉은 두 번 다시 평가되지 못하고 플래그/원장/T+3 후보가 전부
사라진다. 2026-09-16 KIS 실제 플래그 8건 중 4건이 이렇게 없어졌고, 기록된
4건은 전부 재시작 catch-up walk 가 복원한 것이었다:

    08:00 BLUE ✓(재시작)  09:00 RED ✗  09:57 BLUE ✗
    11:09 RED ✓(재배포)   11:12 BLUE ✓ 11:18 RED ✗
    12:30 BLUE ✗          12:45 RED ✓(재배포)

계약
----
  * 실제 완성봉만 평가 (resample/filter/MACD 계산식 무변경)
  * 누락 봉도 FLAG EVENT 로 정상 복원
  * 같은 signal_id 중복 0건
  * **복원된 과거 봉으로 주문 0건** (ORDER RECOVERY 금지)
  * 마지막 봉만 기존 live 경로가 평가하고 주문 권한을 가진다
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, worker
from app.trading.macd2.market_data import filter_complete_3m_bars, resample_completed_3m
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.signal_engine import calculate_macd

KST = config.KST
DAY = datetime(2026, 9, 16, tzinfo=KST)


def _bar(dt, close):
    return {"datetime": dt, "open": close, "high": close,
            "low": close, "close": close, "volume": 1000}


def _series(*, drop_minutes=()):
    """상승->하락->급반등 으로 여러 번 제로크로스가 나는 1분봉 시리즈."""
    rows = []
    price = 1_700_000.0
    t = DAY.replace(hour=9, minute=0)
    for i in range(300):
        if i < 120:
            price += 500.0
        elif i < 200:
            price -= 900.0
        else:
            price += 800.0
        if t.strftime("%H:%M") not in drop_minutes:
            rows.append(_bar(t, price))
        t += timedelta(minutes=1)
    return pd.DataFrame(rows)


def _frame(df_1m, now):
    bars = resample_completed_3m(df_1m, now=now)
    bars, _dropped = filter_complete_3m_bars(bars, df_1m)
    return bars


def _state(last_bar=None):
    s = RuntimeState()
    s.session_date = "20260916"
    if last_bar is not None:
        s.last_confirmed_bar_ts = last_bar.isoformat()
    return s


class _R:
    def __init__(self):
        self.actions = []


def _all_flags(df_1m, now):
    """전 봉을 순회한 '정답' 플래그 집합."""
    bars = _frame(df_1m, now)
    st, out = _state(), []
    last = None
    for i in range(2, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        from app.trading.macd2.signal_engine import evaluate_macd_crossover
        d = evaluate_macd_crossover(snap, last)
        if d != Direction.HOLD:
            last = d
            out.append((snap.bar_dt.astimezone(KST).strftime("%H:%M"), d.value))
    return out


# ══════════════════════════════════════════════════════════════════════════
# A. 핵심 — 늦게 완성된 봉이 반드시 평가된다
# ══════════════════════════════════════════════════════════════════════════
def test_a_bar_that_completed_late_is_still_evaluated():
    """봉 N 이 탈락한 사이 봉 N+1 이 생겨도, N 부터 순서대로 평가한다."""
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    truth = _all_flags(df, now)
    assert len(truth) >= 2, f"시나리오가 플래그를 충분히 못 만든다: {truth}"

    # 첫 플래그 봉 직전까지만 평가된 상태로 만든다
    first_flag_hhmm = truth[0][0]
    idx = next(i for i, d in enumerate(bars["datetime"])
               if pd.Timestamp(d).astimezone(KST).strftime("%H:%M") == first_flag_hhmm)
    st = _state(pd.Timestamp(bars["datetime"].iloc[idx - 1]))

    rec = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())

    # 마지막 봉은 live 경로 몫이므로 제외하고 비교
    last_hhmm = pd.Timestamp(bars["datetime"].iloc[-1]).astimezone(KST).strftime("%H:%M")
    expected = [f for f in truth
                if f[0] >= first_flag_hhmm and f[0] != last_hhmm]
    assert rec == expected, f"복원 집합 불일치\n복원={rec}\n기대={expected}"


def test_multiple_consecutive_late_bars_are_replayed_in_time_order():
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))

    rec = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    times = [t for t, _d in rec]
    assert times == sorted(times), f"시간순이 아니다: {times}"


def test_the_latest_bar_is_never_replayed_here():
    """마지막 봉은 live 경로가 평가해야 한다 -- 주문 권한이 거기에만 있다."""
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    last_dt = pd.Timestamp(bars["datetime"].iloc[-1])
    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))

    worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    assert st.last_confirmed_bar_ts != last_dt.isoformat(), \
        "마지막 봉까지 여기서 평가해버렸다"


# ══════════════════════════════════════════════════════════════════════════
# B. 중복 0건
# ══════════════════════════════════════════════════════════════════════════
def test_replaying_twice_produces_no_duplicate_flags():
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))

    first = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    second = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    assert first, "1회차에서 아무것도 복원되지 않았다"
    assert second == [], f"2회차에서 중복 복원됐다: {second}"

    rows = ledger.load_signal_ledger(limit=10_000)
    ids = [r.get("signal_id") for r in rows]
    assert len(ids) == len(set(ids)), "신호원장에 중복 signal_id 가 있다"


# ══════════════════════════════════════════════════════════════════════════
# C. ORDER RECOVERY 금지
# ══════════════════════════════════════════════════════════════════════════
def test_replay_path_can_never_place_an_order():
    """소스로 고정 -- 이 경로는 broker/order_executor 를 건드리지 않는다."""
    src = inspect.getsource(worker._replay_unevaluated_completed_bars)
    tree = __import__("ast").parse(__import__("textwrap").dedent(src))
    node = tree.body[0]
    if node.body and isinstance(node.body[0], __import__("ast").Expr):
        node.body = node.body[1:]
    code = __import__("ast").unparse(node)
    for forbidden in ("execute_signal", "buy_market", "sell_market",
                      "broker", "_set_pending_signal", "order_executor"):
        assert forbidden not in code, f"복원 경로가 {forbidden} 를 건드린다"


def test_recovered_rows_are_blocked_rows_only():
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))
    rec = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    assert rec
    rows = [r for r in ledger.load_signal_ledger(limit=10_000)
            if str(r.get("block_reason") or "") == worker.LATE_COMPLETED_BAR_REPLAY]
    assert rows, "복원 행이 기록되지 않았다"
    for r in rows:
        assert str(r.get("order_result") or "").upper() != "EXECUTED", \
            f"복원 행이 체결로 기록됐다: {r}"


# ══════════════════════════════════════════════════════════════════════════
# D. 재시작 catch-up 과 순차평가의 플래그 집합이 같다
# ══════════════════════════════════════════════════════════════════════════
def test_live_sequential_replay_matches_the_full_walk():
    df = _series()
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    truth = _all_flags(df, now)

    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))
    rec = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    # 마지막 봉은 live 경로가 처리하므로 그 한 건만 따로 더한다
    snap = calculate_macd(bars)
    d = worker._advance_confirmed_primary(st, snap, now)
    if d != Direction.HOLD:
        rec = rec + [(snap.bar_dt.astimezone(KST).strftime("%H:%M"), d.value)]

    first = truth[0][0]
    expected = [f for f in truth if f[0] >= first]
    assert rec == expected, f"전체 순회와 다르다\n순차={rec}\n전체={expected}"


# ══════════════════════════════════════════════════════════════════════════
# E. 실제 완성봉만 — 불완전 봉은 여전히 제외
# ══════════════════════════════════════════════════════════════════════════
def test_incomplete_bars_are_still_excluded():
    """분봉이 빠진 구간의 3분봉은 복원 대상이 아니다(완성될 때까지)."""
    df = _series(drop_minutes={"10:31"})
    now = DAY.replace(hour=14, minute=0)
    bars = _frame(df, now)
    kept = {pd.Timestamp(d).astimezone(KST).strftime("%H:%M") for d in bars["datetime"]}
    assert "10:30" not in kept, "구성 분봉이 빠졌는데 완성봉으로 취급됐다"

    st = _state(pd.Timestamp(bars["datetime"].iloc[0]))
    rec = worker._replay_unevaluated_completed_bars(
        state=st, bars_3m=bars, now=now, result=_R())
    assert all(t != "10:30" for t, _ in rec), "불완전 봉이 복원됐다"
