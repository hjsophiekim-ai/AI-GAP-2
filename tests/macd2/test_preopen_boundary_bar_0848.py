"""2026-10-21 hotfix: 08:48 3분봉(08:48·08:49 실분봉 + 08:50 padding)을 신호에 인정.

사고 (실계좌 2026-10-21)
-----------------------
KIS 차트: 08:42 DOWN_BLUE / 08:45 UP_RED / 08:48 DOWN_BLUE / 09:00 UP_RED.
production 원장: 08:42 DOWN_BLUE / 08:45 UP_RED(diff +40.93) 뒤로 **09:45 까지 없음**.

10/01 hotfix 가 신호 입력에서 08:50~08:59 padding 1분봉을 빼면서, 08:48 3분봉이
구성분 08:50 결측으로 ``filter_complete_3m_bars`` 에서 **매일** 탈락했다. 그 봉이
없으니 08:48 BLUE 가 사라졌고, 09:00 봉이 08:45 봉(+40.93)과 바로 비교돼 둘 다
양수 -> 09:00 RED 도 교차가 아니게 됐다. 같은 탈락 때문에 order_block_reason 이
하루 종일 HISTORY_GAP 으로 고정돼 있었다(표시만).

데이터 (tests/macd2/data_preopen_padding/)
  hynix_1m_20261020_20261021am.csv : KIS NX 실조회. 10/20 전체(주식일별분봉조회) +
      10/21 08:00~09:20(당일 엔드포인트, 08:50~08:59 padding 포함). production 원장의
      diff(08:42 −68.77 / 08:45 +40.93 / 08:00 +3686.27)를 그대로 재현한다.
  hynix_1m_20260819_20260820am.csv / hynix_1m_20260930_20261001am.csv : 연구 재생
      데이터(전일 엔드포인트 형태, padding 없음) -- 당일 padding 은 테스트에서 덧붙인다.

전략/플래그 정의/청산 규칙은 바꾸지 않는다 -- 바뀌는 것은 '08:48 봉이 완성봉인가'
판정과 HISTORY_GAP 표시 조건뿐이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, signal_engine as se, state_store, worker
from app.trading.macd2.market_data import MarketDataService, _candles_to_df, filter_complete_3m_bars
from app.trading.macd2.models import Direction
from app.trading.macd2.worker import compute_today_signal_overview, run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
DATA = Path(__file__).resolve().parent / "data_preopen_padding"


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=KST)


def _load(name: str) -> pd.DataFrame:
    x = pd.read_csv(DATA / name)
    x["datetime"] = pd.to_datetime(x["datetime"]).dt.tz_localize(KST)
    return x.sort_values("datetime").reset_index(drop=True)


def _with_live_padding(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """당일 엔드포인트 모양: 08:50~08:59 를 직전 종가·0거래량으로 채운다."""
    d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    last = df[df["datetime"] < _ts(f"{d} 08:50")].iloc[-1]
    have = set(df["datetime"])
    pad = pd.DataFrame([{"datetime": _ts(f"{d} 08:{m:02d}"), "open": last.close, "high": last.close,
                         "low": last.close, "close": last.close, "volume": 0}
                        for m in range(50, 60) if _ts(f"{d} 08:{m:02d}") not in have])
    return pd.concat([df, pad]).sort_values("datetime").reset_index(drop=True)


def _flags(df_1m: pd.DataFrame, now: datetime, day: str, *, fixed: bool) -> list[tuple[str, str]]:
    """worker 신호 경로와 같은 순서. fixed=False 는 수정 전(c8a73e1) 입력이다."""
    sig, _ = se.exclude_preopen_padding_1m(df_1m)
    bars = se.resample_completed_3m(sig, now)
    bars, _ = filter_complete_3m_bars(bars, sig, excuse_preopen_padding=fixed)
    bars = bars.reset_index(drop=True)
    last, out = None, []
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
    return out


def _window(flags, lo="08:30", hi="09:10"):
    return [f for f in flags if lo <= f[0] < hi]


B, R = "DOWN_BLUE", "UP_RED"


# ══════════════════════════════════════════════════════════════════════════
# 1. 2026-10-21 실데이터 -- KIS 차트 4개 플래그 그대로
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def frame_1021():
    return _load("hynix_1m_20261020_20261021am.csv")


def test_1021_kis_chart_flags_are_reproduced(frame_1021):
    now = datetime(2026, 10, 21, 9, 12, tzinfo=KST)
    before = _flags(frame_1021, now, "20261021", fixed=False)
    after = _flags(frame_1021, now, "20261021", fixed=True)
    # 수정 전 = 실제 원장(08:45 RED 뒤로 09:45 까지 없음)
    assert _window(before, "08:40") == [("08:42", B), ("08:45", R)]
    # 수정 후 = KIS 차트
    assert _window(after, "08:40") == [("08:42", B), ("08:45", R), ("08:48", B), ("09:00", R)]
    # 08:48 이전 플래그는 그대로
    assert [f for f in before if f[0] < "08:48"] == [f for f in after if f[0] < "08:48"]
    # worker 의 실제 경로(compute_today_signal_overview)가 수정 후 입력을 쓴다
    overview = [(pd.Timestamp(o["bar_start_at"]).strftime("%H:%M"), o["direction"])
                for o in compute_today_signal_overview(frame_1021, now=now, session_started_at=None)]
    assert overview == after


def test_1021_production_ledger_diffs_are_reproduced_exactly(frame_1021):
    """픽스처가 실제 production 프레임과 같다는 근거: 원장 diff 를 소수 둘째 자리까지."""
    sig, _ = se.exclude_preopen_padding_1m(frame_1021)
    bars = se.resample_completed_3m(sig, datetime(2026, 10, 21, 8, 48, 1, tzinfo=KST))
    bars, _ = filter_complete_3m_bars(bars, sig)
    hist = {}
    for i in range(len(bars)):
        snap = se.calculate_macd(bars.iloc[: i + 1])
        if snap is not None:
            hist[snap.bar_dt.strftime("%m%d %H:%M")] = snap.hist_last3[-1]
    assert hist["1021 08:00"] == pytest.approx(3686.27178, abs=0.01)
    assert hist["1021 08:42"] == pytest.approx(-68.770194, abs=0.01)
    assert hist["1021 08:45"] == pytest.approx(40.933516, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════
# 2. 2026-08-20 같은 유형
# ══════════════════════════════════════════════════════════════════════════

def test_0820_same_shape_is_restored():
    df = _with_live_padding(_load("hynix_1m_20260819_20260820am.csv"), "20260820")
    now = datetime(2026, 8, 20, 9, 12, tzinfo=KST)
    before = _window(_flags(df, now, "20260820", fixed=False))
    after = _window(_flags(df, now, "20260820", fixed=True))
    assert ("08:48", R) not in before and ("09:00", B) not in before        # 회귀 재현
    assert ("08:48", R) in after and ("09:00", B) in after                  # 복원


# ══════════════════════════════════════════════════════════════════════════
# 3·4. 10/01 가짜 프리오픈 플래그 / 08:51~08:57 padding 봉 플래그는 계속 없음
# ══════════════════════════════════════════════════════════════════════════

def test_1001_real_day_gets_no_preopen_padding_flag():
    df = _with_live_padding(_load("hynix_1m_20260930_20261001am.csv"), "20261001")
    now = datetime(2026, 10, 1, 9, 12, tzinfo=KST)
    after = _flags(df, now, "20261001", fixed=True)
    assert not [f for f in after if "08:51" <= f[0] < "09:00"], after
    # 08:48 봉을 인정해도 이 날의 프리오픈 구간 플래그는 수정 전과 같다(새 플래그 0)
    assert _window(after) == _window(_flags(df, now, "20261001", fixed=False))


@pytest.mark.parametrize("name,day", [
    ("hynix_1m_20261020_20261021am.csv", "20261021"),
    ("hynix_1m_20260819_20260820am.csv", "20260820"),
    ("hynix_1m_20260930_20261001am.csv", "20261001"),
])
def test_padding_only_bars_never_flag(name, day):
    df = _with_live_padding(_load(name), day)
    now = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:15", tz=KST).to_pydatetime()
    after = _flags(df, now, day, fixed=True)
    assert not [f for f in after if f[0] in ("08:51", "08:54", "08:57")]
    # 같은 봉 중복 플래그 0
    assert len({f[0] for f in after}) == len(after)


def test_filter_unit_contract():
    sig = pd.DataFrame({"datetime": [_ts("2026-10-21 08:48"), _ts("2026-10-21 08:49"),
                                     _ts("2026-10-21 09:00"), _ts("2026-10-21 09:01")],
                        "close": [1.0, 1.0, 1.0, 1.0]})
    bars = pd.DataFrame({"datetime": [_ts("2026-10-21 08:48"), _ts("2026-10-21 08:51"),
                                      _ts("2026-10-21 09:00")], "close": [1.0, 1.0, 1.0]})
    kept, dropped = filter_complete_3m_bars(bars, sig, excuse_preopen_padding=True)
    assert list(kept["datetime"]) == [_ts("2026-10-21 08:48")]        # 08:48 인정
    assert _ts("2026-10-21 08:51") in dropped                          # padding 만 -> 제외
    assert _ts("2026-10-21 09:00") in dropped                          # 09:02 결측은 여전히 결측
    # 08:49 가 실제로 없으면 08:48 봉도 결측
    kept2, _ = filter_complete_3m_bars(bars.iloc[:1], sig.iloc[[0]], excuse_preopen_padding=True)
    assert kept2.empty
    # 옵션 기본값(다른 모듈: TSLA)은 예전 그대로
    kept3, _ = filter_complete_3m_bars(bars.iloc[:1], sig)
    assert kept3.empty


# ══════════════════════════════════════════════════════════════════════════
# 5·6. run_once end-to-end: 원장 1회씩 + HISTORY_GAP 종일 고정 해소
# ══════════════════════════════════════════════════════════════════════════

class _Clock:
    t = datetime(2026, 10, 21, 8, 30, 5, tzinfo=KST)


def _service(frame: pd.DataFrame, clock: _Clock) -> MarketDataService:
    def candles(rows):
        return [{"date": r.datetime.strftime("%Y%m%d"), "time": r.datetime.strftime("%H%M%S"),
                 "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
                for r in rows.itertuples()]

    def fetch_today(mode, symbol, count, hour1):
        rows = frame[(frame["datetime"].dt.strftime("%Y%m%d") == "20261021")
                     & (frame["datetime"] <= pd.Timestamp(clock.t))]
        if hour1:
            rows = rows[rows["datetime"].dt.strftime("%H%M%S") <= hour1]
        return _candles_to_df(candles(rows.tail(30))), {}

    def fetch_date(mode, symbol, date_ymd, count, hour1):
        rows = frame[frame["datetime"].dt.strftime("%Y%m%d") == date_ymd]
        if hour1:
            rows = rows[rows["datetime"].dt.strftime("%H%M%S") <= hour1]
        return _candles_to_df(candles(rows.tail(120))), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fetch_today,
                            fetch_minute_candles_for_date=fetch_date,
                            fetch_quote=lambda mode, symbol: (
                                {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
                                .get(symbol, 1_260_000.0), None))
    assert svc.bootstrap(now=clock.t).ok
    svc.refresh_quotes()
    return svc


def _state():
    s = state_store.default_state()
    s.strategy_version, s.signal_rule = config.STRATEGY_VERSION, config.SIGNAL_RULE
    s.auto_trade_on, s.budget = True, 10_000_000.0
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = True
    return s


def test_1021_run_once_records_each_flag_once_and_history_gap_is_not_pinned(frame_1021):
    clock = _Clock()
    svc = _service(frame_1021, clock)
    state = _state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0,
                                                   config.INVERSE_SYMBOL: 10_000.0})
    reasons = {}
    t = datetime(2026, 10, 21, 8, 31, 3, tzinfo=KST)
    while t <= datetime(2026, 10, 21, 9, 15, 3, tzinfo=KST):
        clock.t = t
        svc.merge_incremental_1m(now=t)
        svc.refresh_quotes()
        run_once(broker=broker, market_data=svc, state=state, now=t)
        reasons[t.strftime("%H:%M")] = state.order_block_reason
        t += timedelta(minutes=1)

    ids = [str(r.get("signal_id") or "") for r in ledger.load_signal_ledger(limit=5000)]
    initial = [i for i in ids if i.startswith("20261021_") and ":" not in i]
    for hhmm, d in (("0842", B), ("0845", R), ("0848", B), ("0900", R)):
        sid = f"20261021_{hhmm}00_{d}"
        assert initial.count(sid) == 1, (sid, initial)
    assert len(initial) == len(set(initial)), "같은 플래그 원장 중복"
    # HISTORY_GAP: 전일 15:18/15:39·당일 08:48 구조적 결측만 있는 날은 표시되지 않는다
    assert not [k for k, v in reasons.items() if v == "HISTORY_GAP"], reasons


def test_history_gap_still_reported_for_a_real_same_day_hole():
    now = datetime(2026, 10, 21, 10, 30, tzinfo=KST)
    structural = [_ts("2026-10-20 15:18"), _ts("2026-10-20 15:39"), _ts("2026-10-21 08:48"),
                  _ts("2026-10-20 12:39")]
    assert worker._actionable_history_gaps(structural, now) == []
    real = _ts("2026-10-21 10:00")
    assert worker._actionable_history_gaps(structural + [real], now) == [real]
    assert worker._actionable_history_gaps([_ts("2026-10-21 15:18")], now) == []
