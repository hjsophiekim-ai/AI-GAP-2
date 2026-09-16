"""09:00 첫 정규장 플래그 누락 방지 (2026-09-16 사고).

배경
----
KIS 는 08:50~08:59 를 거래공백으로 두어 1분봉이 아예 없다. 그래서 08:48 3분봉은
구성 1분봉 결측으로 탈락하고, **08:45 다음 완성봉이 곧바로 09:00** 이 된다.
09:00 봉은 09:03:00 직후에야 완성되는데, 그 시점 첫 worker tick 이 09:02 1분봉을
아직 못 받았으면 09:00 봉은 incomplete 로 탈락한다.

이 파일이 고정하는 계약
----------------------
  CASE A  첫 tick 에서 incomplete -> 평가 안 함, 방향 갱신 없음,
          **last_confirmed_bar_ts 를 09:00 으로 전진시키지 않음**
  CASE B  09:02 1분봉 도착 -> 즉시 재평가 -> UP_RED 1건
  CASE C  같은 데이터로 다시 -> 중복 0건
  CASE D  첫 tick 부터 complete -> 과거와 동일하게 즉시 탐지 (parity)

production 함수만 쓴다(재구현 없음):
``resample_completed_3m`` / ``filter_complete_3m_bars`` / ``calculate_macd`` /
``worker._advance_confirmed_primary``.

MACD 계산식·3분봉 resample 규칙·T+3/quality/TEG/H50/X2-lite/W1a 는 건드리지
않았다. 이 파일은 순수 검증이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, worker
from app.trading.macd2.market_data import filter_complete_3m_bars, resample_completed_3m
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.signal_engine import calculate_macd

KST = config.KST
DAY = datetime(2026, 9, 16, tzinfo=KST)


def _bar(dt: datetime, close: float) -> dict:
    return {"datetime": dt, "open": close, "high": close,
            "low": close, "close": close, "volume": 1000}


def _build_1m(*, include_0902: bool) -> pd.DataFrame:
    """오늘의 실제 데이터 모양: 08:00~08:49 존재, 08:50~08:59 결측, 09:00~ 재개.

    프리마켓은 완만한 하락(먼저 DOWN_BLUE 가 나오도록), 09:00 부터는 강한 상승
    으로 09:00 봉에서 UP_RED 제로크로스가 나오게 만든다. 값 자체를 09:00 에
    맞춘 하드코딩이 아니라, '공백 뒤 첫 봉에서 방향이 바뀌는' 형태를 만든 것이다.
    """
    rows = []
    price = 1_700_000.0
    # 06:00~07:40 : 상승 (diff 를 확실히 + 로 올려둔다)
    t = DAY.replace(hour=6, minute=0)
    while t <= DAY.replace(hour=7, minute=40):
        price += 500.0
        rows.append(_bar(t, price))
        t += timedelta(minutes=1)
    # 07:41~08:49 : 하락 전환 -> 프리마켓에서 DOWN_BLUE 제로크로스가 난다
    while t <= DAY.replace(hour=8, minute=49):
        price -= 700.0
        rows.append(_bar(t, price))
        t += timedelta(minutes=1)
    # 08:50~08:59 : KIS 거래공백 -- 봉 없음
    # 09:00~ : 급반등
    t = DAY.replace(hour=9, minute=0)
    minutes = [0, 1] + ([2] if include_0902 else [])
    for m in minutes:
        price += 20_000.0
        rows.append(_bar(DAY.replace(hour=9, minute=m), price))
    return pd.DataFrame(rows)


def _pipeline(df_1m: pd.DataFrame, now: datetime):
    """production 파이프라인 그대로: resample -> 완성봉 필터 -> MACD."""
    bars = resample_completed_3m(df_1m, now)
    bars, dropped = filter_complete_3m_bars(bars, df_1m)
    return bars, dropped, calculate_macd(bars)


def _state() -> RuntimeState:
    s = RuntimeState()
    s.session_date = "20260916"
    return s


def _warm(state: RuntimeState, df_1m: pd.DataFrame, now: datetime) -> None:
    """09:00 직전까지의 프리마켓 봉들을 정상 tick 처럼 흘려 넣어
    last_detected_direction 을 실제 상태(DOWN_BLUE)로 만든다."""
    bars, _dropped, _snap = _pipeline(df_1m, now)
    for i in range(2, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        bar_dt = snap.bar_dt.astimezone(KST)
        tick = bar_dt + timedelta(minutes=3, seconds=1)
        worker._advance_confirmed_primary(state, snap, tick)


# ══════════════════════════════════════════════════════════════════════════
# 전제: 08:50~08:59 공백이 실제로 08:48 봉을 탈락시키고 09:00 이 다음 봉이다
# ══════════════════════════════════════════════════════════════════════════
def test_the_gap_drops_0848_and_makes_0900_the_next_complete_bar():
    df = _build_1m(include_0902=True)
    now = DAY.replace(hour=9, minute=3, second=30)
    bars, dropped, _snap = _pipeline(df, now)
    starts = [pd.Timestamp(d).strftime("%H:%M") for d in dropped]
    assert "08:48" in starts, f"공백 구간 봉이 탈락하지 않았다: {starts}"
    kept = [pd.Timestamp(d).astimezone(KST).strftime("%H:%M") for d in bars["datetime"]]
    assert "09:00" in kept
    i = kept.index("09:00")
    assert kept[i - 1] == "08:45", f"08:45 다음이 09:00 이 아니다: {kept[i-3:i+1]}"


# ══════════════════════════════════════════════════════════════════════════
# CASE A — 첫 tick 에서 incomplete
# ══════════════════════════════════════════════════════════════════════════
def test_case_a_incomplete_bar_is_not_evaluated_and_never_stamps_0900():
    df_partial = _build_1m(include_0902=False)       # 09:02 아직 없음
    now = DAY.replace(hour=9, minute=3, second=2)
    bars, dropped, snap = _pipeline(df_partial, now)

    starts = [pd.Timestamp(d).strftime("%H:%M") for d in dropped]
    assert "09:00" in starts, "09:02 가 없는데 09:00 봉이 완성으로 취급됐다"
    assert snap is not None
    assert snap.bar_dt.astimezone(KST).strftime("%H:%M") == "08:45", \
        "탈락한 09:00 봉이 snap 이 됐다"

    state = _state()
    _warm(state, df_partial, now)
    before_dir = state.last_detected_direction
    before_flag = state.latest_primary_flag

    d = worker._advance_confirmed_primary(state, snap, now)

    assert d == Direction.HOLD, "incomplete tick 에서 플래그가 생성됐다"
    assert state.last_detected_direction == before_dir
    assert state.latest_primary_flag == before_flag
    # 요구 3: 09:00 으로 전진하면 안 된다
    assert state.last_confirmed_bar_ts != DAY.replace(hour=9, minute=0).isoformat(), \
        "incomplete 인데 09:00 도장이 찍혔다 -- 영구 스킵된다"


# ══════════════════════════════════════════════════════════════════════════
# CASE B / C / D
# ══════════════════════════════════════════════════════════════════════════
def _run_case_b():
    df_partial = _build_1m(include_0902=False)
    df_full = _build_1m(include_0902=True)
    t_a = DAY.replace(hour=9, minute=3, second=2)
    t_b = DAY.replace(hour=9, minute=3, second=7)

    state = _state()
    _warm(state, df_partial, t_a)
    _, _, snap_a = _pipeline(df_partial, t_a)
    worker._advance_confirmed_primary(state, snap_a, t_a)          # CASE A
    prev_dir = state.last_detected_direction

    _, dropped_b, snap_b = _pipeline(df_full, t_b)
    assert "09:00" not in [pd.Timestamp(d).strftime("%H:%M") for d in dropped_b]
    assert snap_b.bar_dt.astimezone(KST).strftime("%H:%M") == "09:00"
    d = worker._advance_confirmed_primary(state, snap_b, t_b)
    return state, snap_b, d, prev_dir, df_full, t_b


def test_case_b_completed_bar_is_re_evaluated_and_produces_the_flag():
    state, snap_b, d, prev_dir, _df, _t = _run_case_b()
    assert prev_dir == Direction.DOWN_BLUE, f"프리마켓 방향이 BLUE 가 아니다: {prev_dir}"
    assert d == Direction.UP_RED, f"완성된 09:00 봉에서 RED 가 안 나왔다: {d}"
    assert state.last_detected_direction == Direction.UP_RED
    assert state.latest_primary_flag == Direction.UP_RED
    assert state.latest_primary_signal_id, "FLAG EVENT signal_id 가 없다"
    # 원장 계약: 봉 시각은 09:00 (탐지 시각이 아니다)
    assert snap_b.bar_dt.astimezone(KST).strftime("%H%M%S") == "090000"


def test_case_c_same_bar_again_produces_no_duplicate_flag():
    state, snap_b, _d, _prev, df_full, _t = _run_case_b()
    sig_before = state.latest_primary_signal_id
    t_c = DAY.replace(hour=9, minute=3, second=12)
    _, _, snap_c = _pipeline(df_full, t_c)
    again = worker._advance_confirmed_primary(state, snap_c, t_c)
    assert again == Direction.HOLD, "같은 09:00 봉에서 중복 플래그가 생성됐다"
    assert state.latest_primary_signal_id == sig_before
    assert state.last_detected_direction == Direction.UP_RED


def test_case_d_already_complete_on_the_first_tick_behaves_exactly_as_before():
    """과거 정상동작 parity — 첫 tick 부터 완성이면 즉시 탐지."""
    df_full = _build_1m(include_0902=True)
    t = DAY.replace(hour=9, minute=3, second=2)
    state = _state()
    _warm(state, df_full, t)
    # _warm 이 이미 09:00 봉을 흘려 넣었으므로 그 결과를 본다
    assert state.last_detected_direction == Direction.UP_RED, \
        "첫 tick 부터 완성인 경우의 탐지가 깨졌다"
    assert state.latest_primary_flag == Direction.UP_RED


def test_case_b_and_case_d_reach_the_same_end_state():
    """늦게 완성되든 처음부터 완성이든 최종 상태가 같아야 한다."""
    state_b, _snap, _d, _p, _df, _t = _run_case_b()

    df_full = _build_1m(include_0902=True)
    state_d = _state()
    _warm(state_d, df_full, DAY.replace(hour=9, minute=3, second=2))

    assert state_b.last_detected_direction == state_d.last_detected_direction
    assert state_b.latest_primary_flag == state_d.latest_primary_flag
    assert state_b.latest_primary_signal_id == state_d.latest_primary_signal_id, \
        "지연 완성과 즉시 완성의 FLAG signal_id 가 다르다 -- 원장이 갈라진다"


# ══════════════════════════════════════════════════════════════════════════
# 공백이 없는 평범한 장중 구간은 아무 것도 달라지지 않는다
# ══════════════════════════════════════════════════════════════════════════
def test_a_normal_intraday_stretch_with_no_gap_is_unchanged():
    rows = []
    price = 1_000_000.0
    t = DAY.replace(hour=10, minute=0)
    for i in range(240):                     # EMA 수렴에 충분한 길이
        price += 400.0 if i < 120 else -700.0
        rows.append(_bar(t, price))
        t += timedelta(minutes=1)
    df = pd.DataFrame(rows)
    now = DAY.replace(hour=14, minute=5)
    bars, dropped, _snap = _pipeline(df, now)
    assert dropped == [], f"공백이 없는데 봉이 탈락했다: {dropped}"

    state = _state()
    flags = []
    for i in range(2, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        tick = snap.bar_dt.astimezone(KST) + timedelta(minutes=3, seconds=1)
        d = worker._advance_confirmed_primary(state, snap, tick)
        if d != Direction.HOLD:
            flags.append((snap.bar_dt.astimezone(KST).strftime("%H:%M"), d.value))
    # 상승->하락 전환이 있으므로 최소 한 건은 나와야 하고, 중복은 없어야 한다
    assert flags, "평범한 구간에서 플래그가 하나도 안 나왔다"
    assert len(flags) == len({f[0] for f in flags}), f"같은 봉 중복 플래그: {flags}"


# ══════════════════════════════════════════════════════════════════════════
# 원장 / UI 계약
# ══════════════════════════════════════════════════════════════════════════
def test_ledger_row_carries_the_bar_time_not_the_recognition_time():
    """09:03:07 에 늦게 탐지돼도 원장의 봉 시각은 09:00 이어야 한다.

    completed_bar_at 을 탐지시각으로 쓰면 같은 봉이 tick 마다 다른 시각으로
    남아 재현/대조가 불가능해진다."""
    import inspect
    src = inspect.getsource(worker._record_signal_ledger)
    assert '"completed_bar_at": macd_snap.bar_dt' in src, \
        "completed_bar_at 이 봉 시각이 아니다"
    assert '"detected_at": detected_at.isoformat()' in src, \
        "detected_at(=인식 시각)이 별도로 기록되지 않는다"


def test_flag_signal_id_is_derived_from_the_bar_not_the_tick():
    """signal_id 가 tick 시각 기반이면 지연 완성 시 중복 행이 생긴다."""
    import inspect
    from app.trading.macd2.worker import make_signal_id
    sig_a = make_signal_id(DAY.replace(hour=9, minute=0), Direction.UP_RED)
    sig_b = make_signal_id(DAY.replace(hour=9, minute=0), Direction.UP_RED)
    assert sig_a == sig_b, "같은 봉/방향인데 signal_id 가 다르다"
    assert "0900" in sig_a, f"signal_id 에 봉 시각이 없다: {sig_a}"


def test_ui_last_flag_event_ignores_order_intent_rows():
    """예약매수/수동진입 같은 '주문 의도' 행이 마지막 FLAG EVENT 를 덮으면
    안 된다 -- 2026-09-16 에 09:03 예약(DOWN_BLUE) 행이 같은 시각의 진짜
    RED 플래그를 가렸다."""
    import importlib.util
    from pathlib import Path as _P
    page = _P(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py"
    src = page.read_text(encoding="utf-8")
    # 렌더를 실행하지 않고 선택 로직만 소스로 고정한다(페이지 import 는 렌더를 탄다)
    assert "_NON_FLAG_SIGNAL_TYPES" in src, "주문 의도 행 제외 목록이 없다"
    for t in ("SCHEDULED_ENTRY_0903", "PREMARKET_CARRY_TW",
              "MANUAL_ENTRY", "MANUAL_LIQUIDATION"):
        assert t in src.split("_NON_FLAG_SIGNAL_TYPES")[1][:400], \
            f"{t} 가 FLAG EVENT 후보에서 제외되지 않았다"
    i = src.index("def _latest_flag_event")
    body = src[i:i + 700]
    assert "_is_flag_event_row" in body, "_latest_flag_event 가 필터를 쓰지 않는다"
