# -*- coding: utf-8 -*-
"""거래별 보유중 가격경로 재구성 + 검증. READ-ONLY.

data/cache 의 1분 OHLCV 를 읽어 진입~청산 사이 경로를 만든다. 이 저장소의
최고 해상도가 1분봉이므로 '틱'은 **1분봉 intrabar high** 로 근사한다
(그 안에서 어느 시점에 닿았는지는 알 수 없다 — 보수적 체결모델로 보완).

검증: 경로의 close 기준 MFE 가 원장 peak_net_pct 와 일치해야 한다
(연구엔진이 quotes.at = 1분 close 로 peak 를 쌓았기 때문).
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np, pandas as pd

sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
from app.trading.macd2.worker import _net_return_pct

PROJ = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2")
CACHE = PROJ / "data" / "cache"
KST = "Asia/Seoul"
TAG = {"0193T0": "long", "0197X0": "inverse"}
_BARS: dict = {}


def bars(symbol: str) -> pd.DataFrame:
    """해당 ETF 의 전 구간 1분 OHLCV (datetime, open, high, low, close)."""
    if symbol in _BARS:
        return _BARS[symbol]
    tag = TAG[symbol]
    fs = sorted(CACHE.glob(f"replay_*_{tag}_1m.csv"))
    fs = [f for f in fs if f.stem.split("_")[1].isdigit() and len(f.stem.split("_")[1]) == 8]
    frames = []
    for f in fs:                      # 파일마다 포맷이 달라 production 처럼 개별 파싱
        d = pd.read_csv(f)
        d["datetime"] = pd.to_datetime(d["datetime"], format="ISO8601")
        if d["datetime"].dt.tz is None:
            d["datetime"] = d["datetime"].dt.tz_localize(KST)
        else:
            d["datetime"] = d["datetime"].dt.tz_convert(KST)
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df = (df.drop_duplicates("datetime", keep="last")
            .sort_values("datetime").reset_index(drop=True))
    _BARS[symbol] = df
    return df


def path(trade) -> pd.DataFrame:
    """진입 시각 **다음 봉**부터 청산 시각까지의 1분봉.

    entry_price 는 진입 시각 봉의 종가(= quotes.at 의 반환값)이므로 그 봉은
    이미 끝났다. 체결 기회는 다음 봉부터다 — 미래정보/선취를 막는 보수적 경계."""
    df = bars(trade["symbol"])
    e = pd.Timestamp(trade["entry_time"])
    x = pd.Timestamp(trade["exit_time"])
    m = (df["datetime"] > e) & (df["datetime"] <= x)
    return df.loc[m].reset_index(drop=True)


def net_of(symbol: str, entry_price: float, px: float) -> float:
    return float(_net_return_pct(symbol, entry_price, px, 1))


def first_touch(trade, thr: float):
    """net >= thr 에 처음 닿는 1분봉. 반환 (idx, row) 또는 (None, None).

    intrabar high 로 판정한다 — 그 봉 안에서 닿았다는 사실만 확실하고
    '언제' 닿았는지는 1분 해상도에서 알 수 없다."""
    p = path(trade)
    if p.empty:
        return None, None
    ep = trade["entry_price"]
    sym = trade["symbol"]
    for i in range(len(p)):
        if net_of(sym, ep, float(p["high"].iloc[i])) >= thr:
            return i, p.iloc[i]
    return None, None


def mfe_close(trade) -> float:
    p = path(trade)
    if p.empty:
        return 0.0
    return max(net_of(trade["symbol"], trade["entry_price"], float(c))
               for c in p["close"])


def mfe_high(trade) -> float:
    p = path(trade)
    if p.empty:
        return 0.0
    return max(net_of(trade["symbol"], trade["entry_price"], float(h))
               for h in p["high"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import k1_core as K
    TS = K.load()
    print(f"거래 {len(TS)}건 경로 재구성 검증")
    miss = [t for t in TS if path(t).empty]
    print(f"  경로 없음 = {len(miss)}건")
    d = [(t, mfe_close(t), t["peak_net_pct"]) for t in TS]
    big = [(t["date"], t["entry_time"][11:16], round(a, 3), round(b, 3))
           for t, a, b in d if abs(a - b) > 0.05]
    print(f"  close 기준 MFE vs 원장 peak_net_pct 불일치(>0.05%p) = {len(big)}건")
    for x in big[:10]:
        print("   ", x)
    err = np.array([a - b for _, a, b in d])
    print(f"  오차 평균 {err.mean():+.4f}%p / 최대 {abs(err).max():.4f}%p")
    hi = np.array([mfe_high(t) - t["peak_net_pct"] for t in TS])
    print(f"  high 기준 MFE - 원장 peak: 평균 {hi.mean():+.3f}%p / 최대 {hi.max():.3f}%p"
          f"  (high 가 더 높은 건 {int((hi > 0).sum())}건 — 정상)")
