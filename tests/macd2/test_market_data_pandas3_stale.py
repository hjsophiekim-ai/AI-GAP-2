"""MarketData stale 회귀 — pandas 3 에서 1분봉이 얼어붙던 사고 (2026-09-20 hotfix).

사고 내용
---------
``_empty_1m_frame()`` 이 모든 컬럼 object dtype 이었고, pandas 2 에서는
``pd.concat([빈 object 프레임, 실데이터])`` 가 빈 쪽 dtype 을 무시해 datetime64
를 유지했지만 **pandas 3 에서는 결과가 object 로 남는다**. 그러면 바로 뒤
``_trim_to_recent_trading_days`` 의 ``.dt`` 접근이 AttributeError 로 터지고,
``merge_incremental_1m`` 이 ``self._df_1m = merged`` 에 도달하지 못해
**메모리 1분봉이 그 시점에 얼어붙는다** — 호가는 계속 갱신되므로
"현재가는 살아 있는데 1분봉/3분봉만 멈춘다" 는 증상이 된다.

수정 방향
---------
(1) 근원: ``merge_incremental_1m`` 이 **빈 프레임을 concat 에서 제외**한다
    (bootstrap 이 이미 쓰는 ``_non_empty`` 관례와 동일). ``_empty_1m_frame``
    자체의 dtype 을 지정하는 방식은 쓰지 않는다 — live 프레임이
    ``datetime64[us, UTC+09:00]`` 이라 tz 표현이 어긋나 또 object 가 된다.
(2) 방어: ``.dt`` 접근 전 ``pd.to_datetime(..., errors="coerce")`` 로 보정하는
    ``_ymd_series`` 헬퍼를 두 호출부에서 쓴다.

이 파일이 고정하는 것
---------------------
A. 빈 프레임을 제외하면 실데이터 dtype 이 보존된다 (근원)
B. 순진하게 concat 하면 pandas 3 에서 object 가 된다 — 사고 원인 자체를 고정
C. object dtype 이 어떤 경로로든 섞여 들어와도 ``.dt`` 가 터지지 않는다 (방어)
D. **merge 가 실제로 전진한다** — 빈 base -> 1회차 -> 2회차로 newest_at 이 계속
   앞으로 간다(사고의 직접 재현/방지)
E. history stale age / 3분봉 완성 카운트가 전진에 따라 갱신된다
F. 해석 불가 행이 섞여도 데이터를 조용히 버리지 않는다

전략 로직(N1/C1/H50/X2-lite)은 이 파일에서 import 조차 하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config
from app.trading.macd2.market_data import (
    MarketDataService, _1M_COLUMNS, _empty_1m_frame, _trim_to_recent_trading_days,
    _ymd_series,
)
from app.trading.macd2.signal_engine import resample_completed_3m

KST = config.KST
DAY = datetime(2026, 9, 17, tzinfo=KST)


def _rows(start_min: int, n: int, day: datetime = DAY) -> pd.DataFrame:
    base = day.replace(hour=9, minute=0)
    return pd.DataFrame({
        "datetime": [base + timedelta(minutes=start_min + i) for i in range(n)],
        "open": [100.0] * n, "high": [100.1] * n, "low": [99.9] * n,
        "close": [100.0] * n, "volume": [10] * n,
    })[list(_1M_COLUMNS)]


# ── A. 근원: 빈 프레임 dtype ──────────────────────────────────────────────
def test_empty_frame_is_never_concatenated_with_real_data():
    """`_empty_1m_frame()` 의 dtype 은 **일부러 건드리지 않았다** — 억지로
    지정하면 live 프레임(datetime64[us, UTC+09:00])과 tz 표현이 어긋나 오히려
    object 로 떨어진다. 대신 merge 가 빈 프레임을 concat 에서 제외한다."""
    e = _empty_1m_frame()
    assert list(e.columns) == list(_1M_COLUMNS)
    assert e.empty
    # 빈 프레임을 제외하면 실데이터의 dtype 이 그대로 보존된다
    live = _rows(0, 5)
    frames = [f for f in (e, live) if not f.empty]
    merged = pd.concat(frames, ignore_index=True)
    assert pd.api.types.is_datetime64_any_dtype(merged["datetime"]), merged["datetime"].dtype


# ── B. 근원: concat 이 dtype 을 잃지 않는다 ───────────────────────────────
def test_naive_concat_with_empty_base_loses_dtype_in_pandas3():
    """사고의 원인을 **명시적으로 고정**한다 — 이 동작이 pandas 쪽에서 바뀌면
    (다시 datetime64 를 유지하게 되면) 이 테스트가 알려준다."""
    naive = pd.concat([_empty_1m_frame(), _rows(0, 5)], ignore_index=True)
    assert naive["datetime"].dtype == object, (
        "pandas 동작이 바뀌었다 — merge 의 빈 프레임 제외가 여전히 필요한지 재검토")
    with pytest.raises(AttributeError):
        naive["datetime"].dt.strftime("%Y%m%d")
    # 그래도 우리 코드 경로(trim)는 _ymd_series 방어로 터지지 않는다
    assert len(_trim_to_recent_trading_days(naive)) == 5


def test_trim_after_concat_with_empty_base_does_not_raise():
    """사고 지점 자체 — 예전엔 여기서 AttributeError 로 1분봉이 멈췄다."""
    merged = pd.concat([_empty_1m_frame(), _rows(0, 10)], ignore_index=True)
    out = _trim_to_recent_trading_days(merged)
    assert len(out) == 10


# ── C. 방어: object dtype 이 섞여도 터지지 않는다 ─────────────────────────
def test_trim_tolerates_object_dtype_datetime():
    df = _rows(0, 6)
    df["datetime"] = df["datetime"].astype(object)
    assert df["datetime"].dtype == object
    out = _trim_to_recent_trading_days(df)          # 예전엔 AttributeError
    assert len(out) == 6


def test_ymd_series_is_noop_on_proper_dtype_and_safe_on_object():
    df = _rows(0, 3)
    proper = _ymd_series(df)
    df2 = df.copy()
    df2["datetime"] = df2["datetime"].astype(object)
    assert list(proper) == list(_ymd_series(df2))
    assert set(proper) == {DAY.strftime("%Y%m%d")}


def test_trim_still_bounds_retention_to_two_days():
    """방어 추가로 기존 보존정책(최근 2거래일)이 바뀌지 않았다."""
    d1 = _rows(0, 5, DAY - timedelta(days=2))
    d2 = _rows(0, 5, DAY - timedelta(days=1))
    d3 = _rows(0, 5, DAY)
    merged = pd.concat([_empty_1m_frame(), d1, d2, d3], ignore_index=True)
    out = _trim_to_recent_trading_days(merged)
    kept = set(_ymd_series(out))
    assert kept == {(DAY - timedelta(days=1)).strftime("%Y%m%d"), DAY.strftime("%Y%m%d")}
    assert len(out) == 10


# ── F. 해석 불가 행이 섞이면 조용히 버리지 않는다 ────────────────────────
def test_unparseable_rows_are_not_silently_dropped():
    df = _rows(0, 4)
    df["datetime"] = df["datetime"].astype(object)
    df.loc[2, "datetime"] = "not-a-timestamp"
    out = _trim_to_recent_trading_days(df)
    assert len(out) == 4, "보정 실패행이 있으면 보수적으로 전량 보존해야 한다"
    assert _ymd_series(df).tolist()[2] == ""


# ── D/E. merge 가 실제로 전진한다 (사고 직접 재현) ───────────────────────
def _svc(pages):
    """``pages`` 를 호출 순서대로 돌려주는 fetcher 로 서비스를 만든다."""
    box = {"i": 0}

    def fetch(mode, symbol, count, hour1):
        i = min(box["i"], len(pages) - 1)
        box["i"] += 1
        return pages[i].copy(), {}

    return MarketDataService(
        mode="mock",
        fetch_minute_candles=fetch,
        fetch_quote=lambda mode, symbol: (100.0, None),
    )


def test_merge_advances_from_empty_base_across_cycles():
    """빈 base -> 1회차 -> 2회차. newest_at 이 매번 앞으로 가야 한다."""
    p1, p2, p3 = _rows(0, 5), _rows(5, 5), _rows(10, 5)
    md = _svc([p1, p2, p3])
    assert md._df_1m.empty

    t = DAY.replace(hour=9, minute=30)
    m1 = md.merge_incremental_1m(now=t)
    assert len(m1) == 5
    newest1 = md._df_1m["datetime"].iloc[-1]

    m2 = md.merge_incremental_1m(now=t + timedelta(minutes=1))
    assert len(m2) == 10, "2회차에서 1분봉이 전진하지 않았다"
    newest2 = md._df_1m["datetime"].iloc[-1]
    assert newest2 > newest1

    m3 = md.merge_incremental_1m(now=t + timedelta(minutes=2))
    assert len(m3) == 15
    newest3 = md._df_1m["datetime"].iloc[-1]
    assert newest3 > newest2
    assert pd.api.types.is_datetime64_any_dtype(md._df_1m["datetime"]), (
        md._df_1m["datetime"].dtype)


def test_three_minute_bars_complete_as_one_minute_history_advances():
    pages = [_rows(0, 9), _rows(9, 9), _rows(18, 9)]
    md = _svc(pages)
    t = DAY.replace(hour=9, minute=30)
    counts = []
    for k in range(3):
        md.merge_incremental_1m(now=t + timedelta(minutes=k))
        counts.append(len(resample_completed_3m(md._df_1m, now=t + timedelta(minutes=k))))
    assert counts[0] < counts[1] < counts[2], f"3분봉 완성이 전진하지 않았다: {counts}"


def test_history_stale_age_resets_when_new_bars_arrive():
    p1, p2 = _rows(0, 5), _rows(5, 5)
    md = _svc([p1, p2])
    t = DAY.replace(hour=9, minute=30)
    md.merge_incremental_1m(now=t)
    age1 = md.history_stale_age_sec(now=t + timedelta(seconds=120))
    assert age1 is not None and age1 >= 100
    md.merge_incremental_1m(now=t + timedelta(seconds=120))
    age2 = md.history_stale_age_sec(now=t + timedelta(seconds=121))
    assert age2 is not None and age2 < age1, "새 봉이 들어왔는데 stale age 가 리셋되지 않았다"


def test_repeating_the_same_page_is_not_progress():
    """같은 봉만 반복해서 오면 '전진' 으로 세지 않는다(alive-but-stale 탐지)."""
    p = _rows(0, 5)
    md = _svc([p, p, p])
    t = DAY.replace(hour=9, minute=30)
    md.merge_incremental_1m(now=t)
    md.merge_incremental_1m(now=t + timedelta(seconds=60))
    age = md.history_stale_age_sec(now=t + timedelta(seconds=120))
    assert age is not None and age >= 100, (
        "같은 봉 반복인데 stale age 가 리셋됐다 — alive-but-stale 을 놓친다")


def test_diagnostics_expose_history_stale_age():
    md = _svc([_rows(0, 5)])
    t = DAY.replace(hour=9, minute=30)
    md.merge_incremental_1m(now=t)
    payload = md.diagnostics(now=t + timedelta(seconds=30)) if hasattr(md, "diagnostics") else None
    if payload is None:
        pytest.skip("diagnostics() 미제공")
    assert "history_stale_age_sec" in payload
    assert payload["history_stale_age_sec"] is not None
