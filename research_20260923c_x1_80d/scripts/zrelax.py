"""zlib — 오후 동일방향 차단 완화 실험 공용. READ-ONLY (production 무수정).

tw3.resolve_slot 을 **런타임 래핑**만 한다. 원본 결정이
REJECT_SAME_DIRECTION_AFTERNOON 일 때에 한해, 사전계산된 봉 특징으로
게이트를 보고 통과시키면 일반 오후 경로(TEG 게이트 필수)와 동일한
SlotDecision 을 돌려준다. 그 외 분기는 원본 그대로 반환한다.
"""
from __future__ import annotations
from dataclasses import replace
import numpy as np, pandas as pd

from app.trading.macd2 import config, time_window_3slot as tw3
from app.trading.macd2.models import Direction

_ORIG = tw3.resolve_slot
FEAT: dict = {}          # (date, "HH:MM") -> dict of features
GATE = None              # callable(feat, direction_value) -> bool
HITS: list = []          # 완화로 열린 후보 로그
RELAXED_KEYS: set = set()  # 완화로 슬롯이 열린 (date, 'HH:MM')


def build_features(bars_3m: pd.DataFrame) -> dict:
    """봉마다 인과적 특징. 전부 '그 봉까지'의 정보만 쓴다."""
    b = bars_3m.reset_index(drop=True).copy()
    b["date"] = b["datetime"].dt.strftime("%Y%m%d")
    out = {}
    fast, slow = int(config.H50_TREND_EMA_FAST), int(config.H50_TREND_EMA_SLOW)
    e_f = b["close"].ewm(span=fast, adjust=False).mean()
    e_s = b["close"].ewm(span=slow, adjust=False).mean()
    gap_pct = (e_f - e_s) / b["close"] * 100.0            # +면 상승추세
    for d, g in b.groupby("date"):
        idx = g.index
        sess = g[(g["datetime"].dt.time >= config.SESSION_OPEN)]
        if sess.empty:
            continue
        tp = (sess["high"] + sess["low"] + sess["close"]) / 3.0
        vol = sess["volume"].replace(0, np.nan)
        vwap = (tp * sess["volume"]).cumsum() / sess["volume"].cumsum()
        run_hi = sess["close"].cummax(); run_lo = sess["close"].cummin()
        day_open = float(sess["close"].iloc[0])
        for pos, i in enumerate(sess.index):
            t = sess["datetime"].loc[i]
            out[(d, t.strftime("%H:%M"))] = dict(
                close=float(sess["close"].loc[i]),
                ema_gap_pct=float(gap_pct.loc[i]),            # 방향 정규화 전
                vwap_dev_pct=(float(sess["close"].loc[i]) / float(vwap.loc[i]) - 1.0) * 100.0,
                new_hi=bool(float(sess["close"].loc[i]) >= float(run_hi.loc[i]) - 1e-9),
                new_lo=bool(float(sess["close"].loc[i]) <= float(run_lo.loc[i]) + 1e-9),
                from_open_pct=(float(sess["close"].loc[i]) / day_open - 1.0) * 100.0,
                bars=pos + 1,
            )
    return out


def _dirsign(direction) -> float:
    v = getattr(direction, "value", direction)
    return 1.0 if str(v).upper().endswith("UP_RED") else -1.0


def patched(*, now, slots_used_today, morning_count, afternoon_count,
            direction, is_flat, last_afternoon_direction=None):
    sd = _ORIG(now=now, slots_used_today=slots_used_today,
               morning_count=morning_count, afternoon_count=afternoon_count,
               direction=direction, is_flat=is_flat,
               last_afternoon_direction=last_afternoon_direction)
    if GATE is None or sd.slot_allowed:
        return sd
    if sd.reject_reason != tw3.REJECT_SAME_DIRECTION_AFTERNOON:
        return sd
    kst = now.astimezone(config.KST)
    key = (kst.strftime("%Y%m%d"), kst.strftime("%H:%M"))
    f = FEAT.get(key)
    if f is None:                     # 특징 없으면 완화하지 않는다(fail-closed)
        return sd
    s = _dirsign(direction)
    feat = dict(f)
    feat["ema_signed"] = s * f["ema_gap_pct"]
    feat["vwap_signed"] = s * f["vwap_dev_pct"]
    feat["extreme"] = f["new_hi"] if s > 0 else f["new_lo"]
    feat["move_signed"] = s * f["from_open_pct"]
    if not GATE(feat):
        return sd
    HITS.append(dict(key=key, direction=getattr(direction, "value", direction), **feat))
    RELAXED_KEYS.add(key)
    return replace(sd, slot_allowed=True, slot_number=slots_used_today + 1,
                   requires_quality_gate=False, requires_teg_gate=True,
                   reject_reason=None)


def install():
    tw3.resolve_slot = patched


def set_gate(fn):
    global GATE
    GATE = fn
    HITS.clear()
    RELAXED_KEYS.clear()
