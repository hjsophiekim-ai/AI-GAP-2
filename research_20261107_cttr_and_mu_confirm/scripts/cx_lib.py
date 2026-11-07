"""오후 BLUE 전환 × 체결강도 지표 A~D — 읽기 전용. production 코드는 import 만 한다.

지표 정의(새 공식·새 임계값 없음, 기준 100 고정):
  A 누적  : KIS 당일 누적 체결강도 = 판정시각 직전 스냅샷의 cttr 값 그대로.
  B 60초  : exec_strength.mean_cttr (production 정의 그대로 — 직전 60초 cttr 평균).
  B' F60  : KIS 체결강도 공식(100 × 매수체결량/매도체결량)을 직전 60초 **창**에 적용.
            창 안 cum 증분으로 계산 — cttr == 100*buy_vol_cum/sell_vol_cum 이 그대로 성립한다.
  C 180초 : 같은 둘을 180초 창으로.
  D 방향  : F60 vs F180 비교. BLUE 기준 F60 < F180 이면 '최근이 BLUE 쪽으로 더 약해짐'.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

import wx_lib as W                              # 같은 디렉터리 — 데이터 루트는 wx_lib 가 쥔다
from app.trading.macd2.models import Direction

NEUTRAL = 100.0


class DaySnap:
    """하루치 000660 1초 스냅샷 — 필요한 4개 컬럼만 올린다."""

    def __init__(self, day: str):
        p = W.rt_root(day) / "snapshots" / day / "000660.csv"
        self.ok = p.exists()
        if not self.ok:
            return
        df = pd.read_csv(p, usecols=["ts", "cttr", "buy_vol_cum", "sell_vol_cum"])
        df["ts"] = pd.to_datetime(df["ts"])
        for c in ("cttr", "buy_vol_cum", "sell_vol_cum"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        self.df = df.dropna().sort_values("ts").reset_index(drop=True)

    def _win(self, end, sec):
        lo = self.df["ts"].searchsorted(end - timedelta(seconds=sec), "left")
        hi = self.df["ts"].searchsorted(end, "left")
        return lo, hi

    def cum(self, end):
        """A — 판정시각 직전 누적 체결강도."""
        hi = self.df["ts"].searchsorted(end, "left")
        return None if hi == 0 else float(self.df["cttr"].iloc[hi - 1])

    def mean_cttr(self, end, sec):
        """B/C(누적평균) — exec_strength.mean_cttr 과 같은 계산(창 길이만 인자)."""
        lo, hi = self._win(end, sec)
        return None if hi <= lo else float(self.df["cttr"].iloc[lo:hi].mean())

    def flow(self, end, sec):
        """B'/C'(창 체결강도) — 창 안 매수/매도 체결량 증분에 KIS 공식 그대로."""
        lo, hi = self._win(end, sec)
        if hi - lo < 2:
            return None
        b = float(self.df["buy_vol_cum"].iloc[hi - 1] - self.df["buy_vol_cum"].iloc[lo])
        s = float(self.df["sell_vol_cum"].iloc[hi - 1] - self.df["sell_vol_cum"].iloc[lo])
        return None if s <= 0 else round(100.0 * b / s, 2)


def match(direction: Direction, v) -> str:
    if v is None:
        return "NA"
    return "MATCH" if ((v > NEUTRAL) if direction == Direction.UP_RED else (v < NEUTRAL)) else "MISMATCH"


def drift(direction: Direction, f60, f180) -> str:
    """D — 최근 60초가 그 방향 쪽으로 더 강해졌는지. 임계값 없이 부호만."""
    if f60 is None or f180 is None:
        return "NA"
    d = f60 - f180
    if d == 0:
        return "FLAT"
    toward = (d > 0) if direction == Direction.UP_RED else (d < 0)
    return "TOWARD" if toward else "AWAY"
