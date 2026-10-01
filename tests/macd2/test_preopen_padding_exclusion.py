"""2026-10-01 hotfix: 08:50~08:59 단일가 padding 봉을 신호 입력에서 제외.

사고: 당일 분봉 엔드포인트가 08:50~08:59 를 직전가·0거래량 평탄봉으로 채워 MACD
histogram 이 0 으로 수렴 -> 08:57 가짜 RED -> last_direction=RED -> 09:00 시가 하락이
새 BLUE 로 잡혀 실계좌 인버스 주문. 수정은 플래그 발행 차단이 아니라 indicator 입력
자체에서 padding 을 빼는 것이므로, 검증도 MACD/플래그/last_direction 수열로 한다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.trading.macd2 import config, signal_engine as se, state_store
from app.trading.macd2.market_data import filter_complete_3m_bars
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.worker import compute_today_signal_overview

KST = config.KST
DATA = Path(__file__).resolve().parent / "data_preopen_padding"


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=KST)


def _replay(df_1m: pd.DataFrame, now: datetime, *, exclude: bool, day: str):
    """worker 와 같은 순서(resample -> filter -> 봉마다 calculate_macd -> crossover).
    exclude=False 가 수정 전(production 8734c3d) 입력이다. 반환: (그날 플래그, 마지막 방향)."""
    if exclude:
        df_1m, _ = se.exclude_preopen_padding_1m(df_1m)
    bars = se.resample_completed_3m(df_1m, now)
    bars, _ = filter_complete_3m_bars(bars, df_1m)
    bars = bars.reset_index(drop=True)
    last = None
    out = []
    for i in range(len(bars)):
        snap = se.calculate_macd(bars.iloc[: i + 1])
        if snap is None:
            continue
        d = se.evaluate_macd_crossover(snap, last)
        if d == Direction.HOLD:
            continue
        last = d
        if bars["datetime"].iloc[i].strftime("%Y%m%d") == day:
            out.append((bars["datetime"].iloc[i].strftime("%H:%M"), d.value))
    return out, last


def _overview(df_1m: pd.DataFrame, now: datetime):
    return [(pd.Timestamp(o["bar_start_at"]).strftime("%H:%M"), o["direction"])
            for o in compute_today_signal_overview(df_1m, now=now, session_started_at=None)]


# ── 단위 ─────────────────────────────────────────────────────────────────
def test_exclude_drops_exactly_0850_to_0859_on_every_day():
    stamps = [_ts(f"2026-10-0{d} {h}") for d in (1, 2)
              for h in ("08:49", "08:50", "08:55", "08:59", "09:00", "09:01")]
    df = pd.DataFrame({"datetime": stamps, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0})
    out, excluded = se.exclude_preopen_padding_1m(df)
    kept = [t.strftime("%d %H:%M") for t in out["datetime"]]
    assert kept == ["01 08:49", "01 09:00", "01 09:01", "02 08:49", "02 09:00", "02 09:01"]
    assert [t.strftime("%d %H:%M") for t in excluded] == ["01 08:50", "01 08:55", "01 08:59",
                                                           "02 08:50", "02 08:55", "02 08:59"]


def test_exclude_passthrough_when_nothing_to_drop_or_malformed():
    assert se.exclude_preopen_padding_1m(None) == (None, [])
    empty = pd.DataFrame(columns=["datetime", "close"])
    assert se.exclude_preopen_padding_1m(empty)[1] == []
    naive = pd.DataFrame({"datetime": pd.to_datetime(["2026-10-01 08:55"]), "close": [1.0]})
    out, ex = se.exclude_preopen_padding_1m(naive)
    assert ex == [] and len(out) == 1          # tz 없는 입력은 건드리지 않는다
    df = pd.DataFrame({"datetime": [_ts("2026-10-01 09:00")], "close": [1.0]})
    out, ex = se.exclude_preopen_padding_1m(df)
    assert ex == [] and out is df              # 제외할 게 없으면 원본 그대로


def test_runtime_state_padding_diag_round_trip():
    st = RuntimeState()
    st.preopen_padding_excluded_count = 10
    st.preopen_padding_last_excluded_at = "2026-10-01T08:59:00+09:00"
    back = state_store.deserialize(state_store.serialize(st))
    assert back.preopen_padding_excluded_count == 10
    assert back.preopen_padding_last_excluded_at == "2026-10-01T08:59:00+09:00"
    assert state_store.deserialize({}).preopen_padding_excluded_count == 0


# ── A. 2026-09-14 실데이터 ───────────────────────────────────────────────
def _load(day: str) -> pd.DataFrame:
    x = pd.read_csv(DATA / f"{day}_hynix_1m.csv")
    x["datetime"] = pd.to_datetime(x["datetime"])
    return x


def _with_live_padding(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """당일 엔드포인트가 주는 모양 그대로: 08:50~08:59 를 직전 종가·0거래량으로."""
    d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    last = df[df["datetime"] < _ts(f"{d} 08:50")].iloc[-1]
    pad = pd.DataFrame([{"datetime": _ts(f"{d} 08:{m:02d}"), "open": last.close, "high": last.close,
                         "low": last.close, "close": last.close, "volume": 0} for m in range(50, 60)])
    return pd.concat([df, pad]).sort_values("datetime").reset_index(drop=True)


@pytest.fixture
def frame_0914():
    if not (DATA / "20260914_hynix_1m.csv").exists():
        pytest.skip("fixture missing")
    raw = pd.concat([_load("20260911"), _load("20260914")]).reset_index(drop=True)
    return raw, _with_live_padding(raw, "20260914")


def test_0914_padding_early_red_is_removed(frame_0914):
    raw, live = frame_0914
    now = datetime(2026, 9, 14, 10, 30, tzinfo=KST)
    before, _ = _replay(live, now, exclude=False, day="20260914")
    after, _ = _replay(live, now, exclude=True, day="20260914")
    # 수정 전 = 실제 사고 (첫 RED 09:12, KIS 차트는 09:21)
    assert [f for f in before if f[1] == "UP_RED"][0] == ("09:12", "UP_RED")
    # 수정 후 = padding 없는 프레임과 같은 09:24, 08:48~08:59 봉 플래그 없음
    assert [f for f in after if f[1] == "UP_RED"][0] == ("09:24", "UP_RED")
    assert not [f for f in after if "08:48" <= f[0] < "09:00"]
    # 08:48 이전 실제 플래그는 그대로
    assert [f for f in before if f[0] < "08:48"] == [f for f in after if f[0] < "08:48"]
    # 같은 시각 중복 플래그 없음
    assert len({f[0] for f in after}) == len(after)
    # worker 의 실제 경로(compute_today_signal_overview)가 수정 후 입력을 쓴다
    assert _overview(live, now) == after


def test_0914_frame_without_padding_is_unchanged(frame_0914):
    raw, _ = frame_0914
    now = datetime(2026, 9, 14, 15, 30, tzinfo=KST)
    assert _replay(raw, now, exclude=False, day="20260914") == _replay(raw, now, exclude=True, day="20260914")


# ── B. 2026-10-01 패턴 (합성: 원장 hist 패턴을 재현하도록 구성) ──────────
def _frame_1001(drop_0900: float = 15000.0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    t, p = _ts("2026-09-30 09:00"), 1800000.0
    while t < _ts("2026-09-30 15:30"):                     # 전일: 횡보 후 막판 상승(RED 상태로 마감)
        p += rng.choice([-1000, 0, 1000]) + (300 if t >= _ts("2026-09-30 15:00") else 0)
        rows.append((t, p))
        t += pd.Timedelta(minutes=1)
    base = p
    for m in range(50):                                    # 당일 08:00~08:49: 급락 후 완만한 하락
        t = _ts("2026-10-01 08:00") + pd.Timedelta(minutes=m)
        p = base - 2500 * (m + 1) if m < 10 else base - 25000 - 150 * (m - 9)
        rows.append((t, round(p, -3)))
    last = rows[-1][1]
    for m in range(50, 60):                                # 08:50~08:59 padding (평탄, 0거래량)
        rows.append((_ts(f"2026-10-01 08:{m}"), last))
    for m in range(30):                                    # 09:00~ 시가 이후 실제 가격
        rows.append((_ts("2026-10-01 09:00") + pd.Timedelta(minutes=m),
                     round(last - drop_0900 * min(m + 1, 6) / 6, -3)))
    df = pd.DataFrame(rows, columns=["datetime", "close"])
    df["open"] = df["close"]
    df["high"] = df["close"] + 500
    df["low"] = df["close"] - 500
    df["volume"] = 100
    pad = df["datetime"].between(_ts("2026-10-01 08:50"), _ts("2026-10-01 08:59"))
    df.loc[pad, ["high", "low"]] = df.loc[pad, "close"]
    df.loc[pad, "volume"] = 0
    return df


def test_1001_phantom_red_and_new_blue_are_removed():
    df = _frame_1001()
    now = datetime(2026, 10, 1, 9, 15, tzinfo=KST)
    before, _ = _replay(df, now, exclude=False, day="20261001")
    after, last = _replay(df, now, exclude=True, day="20261001")
    assert before == [("08:00", "DOWN_BLUE"), ("08:57", "UP_RED"), ("09:00", "DOWN_BLUE")]   # 사고 재현
    assert after == [("08:00", "DOWN_BLUE")]                                                 # 가짜 RED·새 BLUE 없음
    assert last == Direction.DOWN_BLUE                                                       # BLUE 연속 유지
    assert _overview(df, now) == after


# ── C. 정상일: 09:00 이후 실제 방향전환은 그대로 잡는다 ──────────────────
def test_genuine_reversal_after_open_is_still_detected():
    df = _frame_1001(drop_0900=-40000.0)                   # 09:00 시가 이후 강한 상승
    now = datetime(2026, 10, 1, 9, 30, tzinfo=KST)
    after, last = _replay(df, now, exclude=True, day="20261001")
    reds = [f for f in after if f[1] == "UP_RED"]
    assert reds and "09:00" <= reds[0][0] <= "09:09"
    assert last == Direction.UP_RED
