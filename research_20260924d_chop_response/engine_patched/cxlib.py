"""cxlib — CHOP MODE 전략 3종의 **인과적** 특징표와 진입판정. READ-ONLY.

모든 값은 "그 봉까지"의 정보만 쓴다(EWM/누적 VWAP/과거 박스). 미래정보 없음.
진입 판정은 봉 i 가 완성된 뒤 recognition_at = bar_start(i) + 3분 에 일어나므로
봉 i 까지의 값을 쓰는 것이 production 의 T+3 관례와 같다.

임계값은 새로 만들지 않는다 — 전부 **train 구간(5~8월) 분위수**에서만 생성한다.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import Direction

UP, DN = Direction.UP_RED, Direction.DOWN_BLUE


def build(bars_3m: pd.DataFrame, flags_by_idx: dict, train_dates: set) -> dict:
    """봉 단위 인과 특징표. 반환 dict 의 각 배열은 bars 와 같은 길이다."""
    b = bars_3m.reset_index(drop=True).copy()
    dt = pd.to_datetime(b["datetime"])
    date = dt.dt.strftime("%Y%m%d").values
    close = b["close"].astype(float).values
    high = b["high"].astype(float).values
    low = b["low"].astype(float).values
    vol = b["volume"].astype(float).values

    # ── MACD gap (production 정의와 같은 12/26/9 EWM). EWM 은 인과적이다.
    ef = b["close"].ewm(span=12, adjust=False).mean()
    es = b["close"].ewm(span=26, adjust=False).mean()
    macd = ef - es
    sig = macd.ewm(span=9, adjust=False).mean()
    gap = (macd - sig).values
    dgap = np.diff(gap, prepend=gap[0])

    # ── EMA20/50 과 기울기
    e20 = b["close"].ewm(span=20, adjust=False).mean().values
    e50 = b["close"].ewm(span=50, adjust=False).mean().values
    s50 = np.diff(e50, prepend=e50[0])

    # ── 세션 누적 VWAP / 세션 극단 / 박스 (전부 당일 누적, 인과적)
    vwap = np.full(len(b), np.nan)
    sess_hi = np.full(len(b), np.nan)
    sess_lo = np.full(len(b), np.nan)
    box_hi3 = np.full(len(b), np.nan)
    box_lo3 = np.full(len(b), np.nan)
    box_hi_pb = np.full(len(b), np.nan)
    box_lo_pb = np.full(len(b), np.nan)
    tp = (high + low + close) / 3.0
    cur = None
    pv = pvv = 0.0
    hi = lo = None
    for i in range(len(b)):
        if date[i] != cur:
            cur = date[i]
            pv = pvv = 0.0
            hi = lo = None
        v = vol[i] if vol[i] > 0 else 1.0
        pv += tp[i] * v
        pvv += v
        vwap[i] = pv / pvv if pvv else close[i]
        hi = high[i] if hi is None else max(hi, high[i])
        lo = low[i] if lo is None else min(lo, low[i])
        sess_hi[i] = hi
        sess_lo[i] = lo
        # 직전 3봉(현재봉 제외) 박스 — 같은 날 안에서만
        j0 = i - 3
        if j0 >= 0 and date[j0] == date[i] and date[i - 1] == date[i]:
            box_hi3[i] = high[j0:i].max()
            box_lo3[i] = low[j0:i].min()
        # 돌파판정용 박스 — **직전봉(i-1)을 제외**한 3봉(i-4..i-2).
        j1 = i - 4
        if j1 >= 0 and date[j1] == date[i] and date[i - 2] == date[i]:
            box_hi_pb[i] = high[j1:i - 1].max()
            box_lo_pb[i] = low[j1:i - 1].min()

    vwap_dist = (close - vwap) / close * 100.0     # +면 VWAP 위
    # 표준화 VWAP 거리 — 직전 40봉(현재 제외) 표준편차로 나눈다(인과적).
    vd_s = pd.Series(vwap_dist)
    vd_std = vd_s.shift(1).rolling(40, min_periods=20).std().values
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap_z = np.where(np.isfinite(vd_std) & (vd_std > 0), vwap_dist / vd_std, np.nan)

    # ── 직전 확정 플래그 (방향 + 몇 봉 전인가)
    last_flag_dir = np.array([None] * len(b), dtype=object)
    last_flag_age = np.full(len(b), 9999, dtype=int)
    cur_dir, cur_idx = None, None
    cur_day = None
    for i in range(len(b)):
        if date[i] != cur_day:
            cur_day, cur_dir, cur_idx = date[i], None, None
        if i in flags_by_idx:
            cur_dir, cur_idx = flags_by_idx[i], i
        last_flag_dir[i] = cur_dir
        last_flag_age[i] = (i - cur_idx) if cur_idx is not None else 9999

    # ── train 구간 분위수 (9월을 보지 않는다)
    tr = np.array([d in train_dates for d in date])
    q = {}
    vd = np.abs(vwap_z[tr])
    vd = vd[np.isfinite(vd)]
    q["vwap_abs_q80"] = float(np.quantile(vd, 0.80)) if len(vd) else 1.0
    q["vwap_abs_q85"] = float(np.quantile(vd, 0.85)) if len(vd) else 1.2
    ag = np.abs(gap[tr])
    ag = ag[np.isfinite(ag)]
    q["gap_abs_med"] = float(np.quantile(ag, 0.50)) if len(ag) else 0.0

    return dict(date=date, close=close, high=high, low=low, gap=gap, dgap=dgap,
                e20=e20, e50=e50, s50=s50, vwap=vwap, vwap_dist=vwap_dist,
                sess_hi=sess_hi, sess_lo=sess_lo, box_hi3=box_hi3, box_lo3=box_lo3,
                box_hi_pb=box_hi_pb, box_lo_pb=box_lo_pb, vwap_z=vwap_z,
                last_flag_dir=last_flag_dir, last_flag_age=last_flag_age, q=q)


def _sgn(d) -> float:
    return 1.0 if d == UP else -1.0


def evaluate(strategy: str, F: dict, i: int, params: dict) -> Optional[Direction]:
    """봉 i 가 완성된 시점의 진입 판정. 진입 방향 또는 None."""
    if i < 5 or not np.isfinite(F["box_hi3"][i]):
        return None
    c = F["close"][i]
    gap, dgap = F["gap"][i], F["dgap"][i]
    vd = F["vwap_dist"][i]
    q = F["q"]

    if strategy == "C1":
        # SHORT CONTINUATION — 직전 flag 방향으로 gap 확대 + 박스 상방돌파 + VWAP 우호
        d = F["last_flag_dir"][i]
        if d is None or F["last_flag_age"][i] > int(params.get("age_max", 6)):
            return None
        s = _sgn(d)
        if s * dgap <= 0:                      # 해당 방향으로 gap 확대
            return None
        if s > 0 and not (c > F["box_hi3"][i]):
            return None
        if s < 0 and not (c < F["box_lo3"][i]):
            return None
        if s * vd <= 0:                        # VWAP 우호(위/아래)
            return None
        return d

    if strategy == "C2":
        # FAILED BREAKOUT REVERSAL — 직전봉이 박스를 깼는데 이번봉이 박스 안으로 복귀
        bh, bl = F["box_hi_pb"][i], F["box_lo_pb"][i]
        ph, pl, pc = F["high"][i - 1], F["low"][i - 1], F["close"][i - 1]
        if not np.isfinite(bh) or not np.isfinite(bl):
            return None
        up_fail = (ph > bh) and (c <= bh) and (pc >= bl)
        dn_fail = (pl < bl) and (c >= bl) and (pc <= bh)
        if not (up_fail or dn_fail):
            return None
        # gap 축소 또는 반전
        if up_fail:
            if dgap >= 0:
                return None
            return DN
        if dgap <= 0:
            return None
        return UP

    if strategy == "C3":
        # VWAP MEAN REVERSION — 극단 이탈 + gap 둔화 + slope 둔화 + 극단 미갱신
        th = q["vwap_abs_q80"] if params.get("qtl", 80) == 80 else q["vwap_abs_q85"]
        vz = F["vwap_z"][i]
        if not np.isfinite(vz) or abs(vz) < th:
            return None
        if abs(gap) > q["gap_abs_med"] and abs(F["dgap"][i]) > abs(F["dgap"][i - 1]):
            return None                        # 아직 가속 중이면 진입 금지
        if abs(F["s50"][i]) > abs(F["s50"][i - 1]):
            return None                        # 기울기가 더 가팔라지면 금지
        if vz > 0 and F["high"][i] >= F["sess_hi"][i - 1]:
            return None                        # 세션 고점 갱신 중이면 금지
        if vz < 0 and F["low"][i] <= F["sess_lo"][i - 1]:
            return None
        return DN if vz > 0 else UP            # VWAP 쪽으로

    return None
