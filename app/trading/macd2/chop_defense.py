"""CHOP-STACK — 왕복장 방어모드의 세 규칙 (2026-10-11).

연구: ``research_20261011b_chop_stack`` (REAL replay 9~10월 23일 + 85일 42영향일).

P3(=E) 위에 **진입 억제 3종**만 얹는다. 청산 래더·사이징·TEG/VWAP/품질 게이트는
P3 와 100% 같다.

    ① 2SLOT      하루 최대 진입 3 -> 2                      :func:`daily_cap`
    ② MC-DELAY   오전 왕복장 구간 통째 no-trade(슬롯 미소비)  :func:`morning_no_trade`
    ③ 구조손절    B3 -1% 즉시손절 -> 박스 확정이탈이면 손절    :func:`b3_stop_action`

세 규칙 전부 :func:`is_active` 가 False 면 **한 줄도 돌지 않는다** — P3/N1 의
동작은 조금도 바뀌지 않는다.

박스 정의는 ②의 해제판정과 ③의 구조이탈 판정이 **같은 것을 쓴다**:
최근 ``CHOP_BOX_MINUTES`` 분 중 마지막 ``CHOP_BOX_SKIP`` 분을 뺀 구간의
완성 1분봉 high/low. 연구 b/c 에서 사전 고정한 정의 그대로다.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config
from .models import Direction

__all__ = [
    "is_active", "daily_cap", "morning_no_trade", "advance_mc", "b3_stop_action",
    "note_morning_flag", "note_bars", "cached_box", "reset_daily",
    "STOP_EXECUTE", "STOP_DEFER",
]

#: :func:`b3_stop_action` 반환값.
STOP_EXECUTE = "EXECUTE"      # -1% 손절을 그대로 집행
STOP_DEFER = "DEFER"          # 유예 (worker 는 HOLD 로 넘긴다)


# ─────────────────────────────────────────────────────────────────────────
# 활성 판정
# ─────────────────────────────────────────────────────────────────────────
def is_active(state: Any) -> bool:
    """이 tick 에서 CHOP-STACK 규칙이 적용되는가.

    ``strategy_mode.apply`` 가 찍는 ``state.chop_defense_enabled`` 하나만 본다.
    P3/N1 모드에서는 False 라 아래 함수들이 전부 no-op 이다.
    """
    return bool(getattr(state, "chop_defense_enabled", False))


# ─────────────────────────────────────────────────────────────────────────
# ① 2SLOT
# ─────────────────────────────────────────────────────────────────────────
def daily_cap(state: Any) -> int:
    """이 모드의 하루 최대 진입 수."""
    if is_active(state):
        return int(config.CHOP_DAILY_CAP)
    return int(config.TW2_3SLOT_DAILY_CAP)


# ─────────────────────────────────────────────────────────────────────────
# 공통 — 1분봉 유틸
# ─────────────────────────────────────────────────────────────────────────
def _completed_intraday(df_1m: Optional[pd.DataFrame], now: datetime) -> Optional[pd.DataFrame]:
    """당일 09:00 이후의 **완성** 1분봉만. 미완성 봉은 보지 않는다."""
    if df_1m is None or not len(df_1m):
        return None
    d = df_1m
    if "datetime" not in d.columns:
        return None
    ts = pd.to_datetime(d["datetime"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(config.KST) if ts.dt.tz is not None else ts
    cur = pd.Timestamp(now)
    if cur.tzinfo is not None and getattr(ts.dt, "tz", None) is None:
        cur = cur.tz_localize(None)
    elif cur.tzinfo is None and getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    work = d.assign(_ts=ts)
    # 완성봉 = 봉 시작 + 1분 <= now
    work = work[work["_ts"] + pd.Timedelta(minutes=1) <= cur]
    work = work[work["_ts"].dt.date == cur.date()]
    hm = work["_ts"].dt.strftime("%H%M")
    work = work[hm >= "0900"]
    return work if len(work) else None


def _box(work: pd.DataFrame) -> Optional[tuple[float, float, float]]:
    """(상단, 하단, 중앙) — 최근 CHOP_BOX_MINUTES 분 중 마지막 CHOP_BOX_SKIP 분 제외."""
    n = int(config.CHOP_BOX_MINUTES)
    skip = int(config.CHOP_BOX_SKIP)
    if len(work) <= skip:
        return None
    seg = work.iloc[-n:]
    seg = seg.iloc[:-skip] if len(seg) > skip else seg
    if not len(seg):
        return None
    hi = float(seg["high"].max())
    lo = float(seg["low"].min())
    return hi, lo, (hi + lo) / 2.0


def note_bars(state: Any, now: datetime, df_1m: Optional[pd.DataFrame]) -> None:
    """틱마다 1분봉 파생값을 state 에 캐시한다 (run_once 상단에서 한 번).

    ``df_1m`` 은 ``run_once`` 에만 있고 B3 판정부(_advance_held_position_risk_management /
    _advance_p3_max_hold)까지 내려가지 않는다. 시그니처를 여러 단계 바꾸는 대신
    **박스와 마지막 완성봉 종가만** 여기서 캐시해 그쪽이 읽게 한다.

    같은 분(minute)에 두 번 불려도 한 번만 계산한다. CHOP 모드가 아니면 no-op.
    """
    if not is_active(state):
        return
    stamp = now.strftime("%Y%m%d%H%M")
    if str(getattr(state, "chop_bars_at", "") or "") == stamp:
        return
    state.chop_bars_at = stamp
    work = _completed_intraday(df_1m, now)
    if work is None:
        state.chop_box = None
        state.chop_last_close = None
        return
    box = _box(work)
    state.chop_box = list(box) if box else None
    state.chop_last_close = float(work["close"].to_numpy(float)[-1])


def cached_box(state: Any) -> Optional[tuple[float, float, float]]:
    box = getattr(state, "chop_box", None)
    if not box or len(box) != 3:
        return None
    return float(box[0]), float(box[1]), float(box[2])


# ─────────────────────────────────────────────────────────────────────────
# ② MC-DELAY
# ─────────────────────────────────────────────────────────────────────────
def reset_daily(state: Any) -> None:
    """날이 바뀌면 오전 플래그 기록과 ON/OFF 상태를 비운다."""
    state.chop_mc_flags = []
    state.chop_mc_on_at = None
    state.chop_mc_off_at = None
    state.chop_mc_day = None
    state.chop_mc_off_eval_at = None
    state.chop_mc_on_eval = None
    state.chop_bars_at = None
    state.chop_box = None
    state.chop_last_close = None


def _ensure_day(state: Any, now: datetime) -> None:
    day = now.strftime("%Y%m%d")
    if str(getattr(state, "chop_mc_day", "") or "") != day:
        reset_daily(state)
        state.chop_mc_day = day


def note_morning_flag(state: Any, now: datetime, direction: Any) -> None:
    """09:00~CHOP_MC_WINDOW_END 사이에 뜬 **원 플래그**를 기록한다.

    방향전환 횟수를 세기 위한 것이고, 모드와 무관하게 기록만 한다(가볍다) —
    사용자가 장중에 모드를 켜도 그 시점의 오전 이력이 남아 있도록.
    """
    if direction is None:
        return
    _ensure_day(state, now)
    if now.timetz().replace(tzinfo=None) > config.CHOP_MC_WINDOW_END:
        return
    val = getattr(direction, "value", str(direction))
    flags = list(getattr(state, "chop_mc_flags", None) or [])
    stamp = now.strftime("%H%M")
    if flags and flags[-1][0] == stamp and flags[-1][1] == val:
        return
    flags.append([stamp, val])
    state.chop_mc_flags = flags


def _reversals(state: Any) -> int:
    flags = list(getattr(state, "chop_mc_flags", None) or [])
    rev = 0
    for i in range(1, len(flags)):
        if flags[i][1] != flags[i - 1][1]:
            rev += 1
    return rev


def _path_is_chop(work: pd.DataFrame) -> bool:
    """최근 CHOP_MC_LOOKBACK_MIN 분의 PE 와 EMA20 교차 횟수 (연구 mc1.chop_path 와 동일)."""
    n = int(config.CHOP_MC_LOOKBACK_MIN)
    seg = work.iloc[-n:]
    if len(seg) < int(config.CHOP_MC_MIN_BARS):
        return False
    c = seg["close"].to_numpy(float)
    moved = float(np.abs(np.diff(c)).sum())
    pe = abs(float(c[-1] - c[0])) / moved if moved else 0.0
    ema = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    sign = np.sign(c - ema)
    sign = sign[sign != 0]
    xc = int((np.diff(sign) != 0).sum()) if len(sign) > 1 else 0
    return bool(pe < float(config.CHOP_MC_PE_MAX) and xc >= int(config.CHOP_MC_XC_MIN))


def _ema(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).ewm(span=n, adjust=False).mean().to_numpy()


def _macd_gap_3m_last(work: pd.DataFrame) -> Optional[float]:
    """당일 09:00 이후 완성 1분봉 종가 -> 3분 리샘플(last) -> MACD(12,26,9) gap 의 마지막 값.

    연구 ``feat.macd_gap_3m`` 과 같은 계산이다(워밍업 없이 09:00 부터).
    """
    if work is None or not len(work):
        return None
    s = pd.Series(work["close"].to_numpy(float), index=pd.DatetimeIndex(work["_ts"]))
    g = s.resample("3min").last().dropna()
    if not len(g):
        return None
    a = g.to_numpy(float)
    m = _ema(a, 12) - _ema(a, 26)
    return float((m - _ema(m, 9))[-1])


def _trend_ok(work: pd.DataFrame, up: bool) -> bool:
    """MC 해제용 한 방향 돌파확인 — 연구 mc1.trend_ok 와 동일한 5조건.

    work 의 마지막 행이 판정 봉(i)이다.
      박스 = 최근 CHOP_BOX_MINUTES 분 중 마지막 CHOP_BOX_SKIP 분 제외의 high/low
      (a) 마지막 CHOP_BREAKOUT_BARS 개 종가가 모두 박스 밖
      (b) 마지막 봉이 직전 CHOP_BREAKOUT_HHLL 봉 극값을 신호방향으로 갱신
      (c) 종가가 박스 중앙을 되밟지 않았다
      (d) 3분봉 MACD gap 부호가 방향과 일치
    """
    boxw = int(config.CHOP_BOX_MINUTES)
    if len(work) < boxw + 3:                      # 연구: i < BOXW + 2 -> False
        return False
    box = _box(work)
    if box is None:
        return False
    bh, bl, mid = box
    nout = int(config.CHOP_BREAKOUT_BARS)
    look = int(config.CHOP_BREAKOUT_HHLL)
    close = work["close"].to_numpy(float)
    high = work["high"].to_numpy(float)
    low = work["low"].to_numpy(float)
    bound = bh if up else bl
    last = close[-nout:]
    if not (bool((last > bound).all()) if up else bool((last < bound).all())):
        return False
    if up:
        if not bool(high[-1] > high[-1 - look:-1].max()):
            return False
        if not bool(close[-1] >= mid):
            return False
    else:
        if not bool(low[-1] < low[-1 - look:-1].min()):
            return False
        if not bool(close[-1] <= mid):
            return False
    gap = _macd_gap_3m_last(work)
    return bool(gap is not None and ((gap > 0) if up else (gap < 0)))


def _rev_at(state: Any) -> Optional[str]:
    """방향전환이 CHOP_MC_REVERSALS 회째가 된 플래그의 시각(HHMM). 아직이면 None."""
    flags = list(getattr(state, "chop_mc_flags", None) or [])
    rev = 0
    for i in range(1, len(flags)):
        if flags[i][1] != flags[i - 1][1]:
            rev += 1
            if rev >= int(config.CHOP_MC_REVERSALS):
                return str(flags[i][0])
    return None


def advance_mc(state: Any, now: datetime, df_1m: Optional[pd.DataFrame]) -> None:
    """MC-DELAY ON/OFF 를 **완성 1분봉마다** 판정해 state 에 고정한다 (tick 마다 호출).

    연구 ``research_20261011b_chop_stack/scripts/mc1.py`` 와 같은 규칙이다.

    ON  : 방향전환 CHOP_MC_REVERSALS 회째 플래그의 봉 시각 이후 ~ CHOP_MC_WINDOW_END 의
          각 완성봉에서, 그 봉까지의 최근 60분 PE/EMA20 교차가 왕복장이면 그 봉에서 ON.
          플래그는 확정된 뒤에야 기록되므로, 기록 시점보다 앞선 봉도 다시 훑는다
          (봉 시각 기준으로 연구와 같은 첫 봉을 찾는다 — 차단 효과는 지금부터).
    OFF : ON 이후, CHOP_MC_RELEASE_FROM 이상인 각 완성봉에서 UP/DOWN 돌파확인
          (:func:`_trend_ok`, MACD gap 부호 포함)이 처음 성립하면 그 봉에서 OFF.
          한 번 OFF 면 그 날은 다시 ON 되지 않는다.

    CHOP 모드가 아니면 no-op.
    """
    if not is_active(state) or not bool(config.CHOP_MC_ENABLED):
        return
    _ensure_day(state, now)
    if getattr(state, "chop_mc_off_at", None):
        return
    work = _completed_intraday(df_1m, now)
    if work is None:
        return
    hms = work["_ts"].dt.strftime("%H%M").to_numpy()
    win_end = config.CHOP_MC_WINDOW_END.strftime("%H%M")
    rel_from = config.CHOP_MC_RELEASE_FROM.strftime("%H%M")

    if not getattr(state, "chop_mc_on_at", None):
        rev_at = _rev_at(state)
        if rev_at is None:
            return
        # 이미 같은 rev_at 으로 훑은 봉은 다시 보지 않는다(완성봉은 바뀌지 않는다).
        # rev_at 이 새로 생기면 그 시각부터 다시 훑는다.
        cur = str(getattr(state, "chop_mc_on_eval", "") or "")
        c_rev, _, c_last = cur.partition("|")
        start_after = c_last if c_rev == rev_at else ""
        for i in range(len(work)):
            hm = hms[i]
            if hm < rev_at or hm > win_end or (start_after and hm <= start_after):
                continue
            state.chop_mc_on_eval = f"{rev_at}|{hm}"
            if _path_is_chop(work.iloc[:i + 1]):
                state.chop_mc_on_at = str(hm)
                break
        if not getattr(state, "chop_mc_on_at", None):
            return

    # OFF 판정 — 이미 본 봉은 다시 보지 않는다(완성봉은 바뀌지 않으므로 결과도 같다).
    seen = str(getattr(state, "chop_mc_off_eval_at", "") or "")
    for i in range(len(work)):
        hm = hms[i]
        if hm < rel_from or (seen and hm <= seen):
            continue
        sub = work.iloc[:i + 1]
        hit = next((up for up in (True, False) if _trend_ok(sub, up)), None)
        state.chop_mc_off_eval_at = str(hm)
        if hit is not None:
            state.chop_mc_off_at = str(hm)
            return


def morning_no_trade(state: Any, now: datetime, df_1m: Optional[pd.DataFrame],
                     gap_up: Optional[bool] = None) -> bool:
    """지금 MC-DELAY 가 신규진입을 막는 구간인가 (ON 이후 ~ OFF 전).

    판정 자체는 :func:`advance_mc` 가 완성봉마다 한다. 여기서는 최신화만 하고
    상태를 읽는다. ``gap_up`` 은 하위호환용 인자로 쓰지 않는다(gap 은 1분봉에서 직접 계산).

    **신규진입만** 막는다 — 보유 포지션의 반대신호 청산은 호출부(worker)가 그대로
    진행한다(2026-10-12 결정: production-safe MC).
    """
    if not is_active(state) or not bool(config.CHOP_MC_ENABLED):
        return False
    advance_mc(state, now, df_1m)
    return bool(getattr(state, "chop_mc_on_at", None)) and not bool(getattr(state, "chop_mc_off_at", None))


# ─────────────────────────────────────────────────────────────────────────
# ③ 구조손절
# ─────────────────────────────────────────────────────────────────────────
def b3_stop_action(state: Any, *, now: datetime, entry_at: Optional[datetime],
                   net_return_pct: float, direction: Any) -> str:
    """B3 -1% 손절이 떴을 때 집행할지 유예할지.

    * 박스를 포지션 **반대방향으로 확정이탈** -> :data:`STOP_EXECUTE` (구조적 손절)
    * 판정 불가(봉 부족) -> :data:`STOP_EXECUTE` (fail-safe = 현행)
    * net >= CHOP_STOP_RECOVER_PCT 로 회복 -> :data:`STOP_EXECUTE` (회복 청산)
    * 첫 터치 + CHOP_STOP_DEFER_MIN 경과 또는 진입 + B3 max-hold 경과
      -> :data:`STOP_EXECUTE`
    * 그 외 -> :data:`STOP_DEFER`

    유예 중에도 TW 손절/반대신호/C1/H50/TP 는 전부 살아 있다 — 이 함수는
    **B3 의 -1% 한 경로만** 가로챈다.
    """
    if not is_active(state) or not bool(config.CHOP_STOP_DEFER_ENABLED):
        return STOP_EXECUTE
    if entry_at is None:
        return STOP_EXECUTE
    key = entry_at.isoformat()
    if str(getattr(state, "chop_stop_key", "") or "") != key:
        state.chop_stop_key = key
        state.chop_stop_first_at = now.isoformat()
    up = (direction == Direction.UP_RED)
    box = cached_box(state)
    px = getattr(state, "chop_last_close", None)
    if box is None or px is None:
        return STOP_EXECUTE                      # 판정 불가 -> 기존 손절 그대로
    bh, bl, _mid = box
    px = float(px)
    if (px < bl) if up else (px > bh):
        return STOP_EXECUTE                      # 구조적 이탈
    if float(net_return_pct) >= float(config.CHOP_STOP_RECOVER_PCT):
        return STOP_EXECUTE                      # 회복 청산
    first = getattr(state, "chop_stop_first_at", None)
    try:
        first_dt = datetime.fromisoformat(str(first)) if first else now
    except ValueError:
        first_dt = now
    waited = (now - first_dt).total_seconds() / 60.0
    held = (now - entry_at).total_seconds() / 60.0
    if waited >= float(config.CHOP_STOP_DEFER_MIN):
        return STOP_EXECUTE
    if held >= float(config.P3_B3_MAX_HOLD_MIN):
        return STOP_EXECUTE
    return STOP_DEFER
