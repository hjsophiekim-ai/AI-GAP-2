# -*- coding: utf-8 -*-
"""[G section2] +1.5% 도달 '그 시점'에 알 수 있던 feature 비교. READ-ONLY.

미래정보 차단 규칙 (전부 코드로 강제):
  * 하이닉스 3분봉은 `datetime + 3min <= 도달봉 시각 + 1min` 인 **완성봉**만.
    (도달봉은 1분봉이고, 그 봉이 끝나는 시각까지 완성된 3분봉만 본다)
  * ETF 1분봉은 도달봉 **포함 그 이전**까지만.
  * MACD/EMA 는 adjust=False 재귀식이라 인과적 — 전 구간 1회 계산 후
    해당 인덱스를 읽는 것과 잘라서 계산하는 것이 동일하다.
"""
from __future__ import annotations
import sys, pickle
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import t1_path as P
from app.trading.macd2 import config as C
from app.trading.macd2 import n1_adaptive as NA
from app.trading.macd2.models import Direction
from g1_extract import ROWS, THR, cls_def1, cls_def2, W30

B3 = pickle.load(open("_ctx_B.pkl", "rb"))["hynix_bars_3m"].reset_index(drop=True)
_c = pd.to_numeric(B3["close"], errors="coerce")
_ef = _c.ewm(span=C.EMA_FAST, adjust=False).mean()
_es = _c.ewm(span=C.EMA_SLOW, adjust=False).mean()
MACD = (_ef - _es)
SIG = MACD.ewm(span=C.EMA_SIGNAL, adjust=False).mean()
HIST = (MACD - SIG).to_numpy()
EF20 = _c.ewm(span=C.H50_TREND_EMA_FAST, adjust=False).mean().to_numpy()
ES50 = _c.ewm(span=C.H50_TREND_EMA_SLOW, adjust=False).mean().to_numpy()
BT = pd.DatetimeIndex(B3["datetime"])
CL = _c.to_numpy()


def last_done_idx(ts) -> int:
    """ts 시점에 **완성되어 있는** 마지막 3분봉 인덱스 (없으면 -1)."""
    lim = pd.Timestamp(ts) - pd.Timedelta(minutes=3)
    return int(BT.searchsorted(lim, side="right") - 1)


def feats(r) -> dict:
    sym, ep = r["symbol"], r["entry_price"]
    pth = P.path(r)
    i = r["touch_idx"]
    bar_end = pd.Timestamp(pth["datetime"].iloc[i]) + pd.Timedelta(minutes=1)
    k = last_done_idx(bar_end)
    up = r["direction"] == "UP_RED"
    f = {}

    # --- 시간/속도 ---
    f["도달분"] = r["touch_min"]
    f["속도%분"] = THR / max(1, r["touch_min"])
    f["도달시각"] = int(r["touch_time"][11:13]) * 60 + int(r["touch_time"][14:16])
    f["오전"] = 1 if r["session"] == "MORNING" else 0
    f["slot"] = r["slot"]
    f["UP_RED"] = 1 if up else 0
    f["entry_chop"] = 1 if r["entry_chop"] else 0

    # --- 하이닉스(감시종목) 구조, 완성봉만 ---
    if k >= 2:
        h0, h1, h2 = HIST[k], HIST[k - 1], HIST[k - 2]
        sgn = 1.0 if up else -1.0
        f["gap_dir"] = sgn * h0                     # 보유방향 기준 부호화 gap
        f["gap_축소"] = 1 if abs(h0) < abs(h1) else 0
        f["gap_2연속축소"] = 1 if (abs(h0) < abs(h1) < abs(h2)) else 0
        f["gap_역전"] = 1 if sgn * h0 < 0 else 0
        f["gapΔ_dir"] = sgn * (h0 - h1)
        f["ema20_50%"] = sgn * (EF20[k] - ES50[k]) / CL[k] * 100.0
        f["N1추세ok"] = 1 if NA.snapshot(
            B3.iloc[: k + 1], Direction.UP_RED if up else Direction.DOWN_BLUE).ok else 0
    else:
        for kk in ("gap_dir", "gap_축소", "gap_2연속축소", "gap_역전",
                   "gapΔ_dir", "ema20_50%", "N1추세ok"):
            f[kk] = np.nan

    # --- ETF 1분봉 (도달봉 포함, 그 이전만) ---
    seg = pth.iloc[: i + 1]
    o = float(seg["open"].iloc[-1]); h = float(seg["high"].iloc[-1])
    l = float(seg["low"].iloc[-1]); c = float(seg["close"].iloc[-1])
    rngv = max(h - l, 1e-9)
    f["위꼬리비"] = (h - max(o, c)) / rngv
    f["종가위치"] = (c - l) / rngv                   # 1=고가마감, 0=저가마감
    f["도달봉net종가"] = P.net_of(sym, ep, c)        # 닿았다가 되밀렸나
    f["되밀림"] = THR - f["도달봉net종가"]
    up_run = 0
    for j in range(len(seg) - 1, 0, -1):
        if float(seg["close"].iloc[j]) > float(seg["close"].iloc[j - 1]):
            up_run += 1
        else:
            break
    f["연속상승봉"] = up_run
    v = pd.to_numeric(seg["volume"], errors="coerce").to_numpy(dtype=float)
    prev = v[-21:-1]
    f["거래량배"] = (v[-1] / prev.mean()) if len(prev) >= 5 and prev.mean() > 0 else np.nan
    f["도달전MAE"] = r["mae_before"]
    return f


FE = {(r["date"], r["entry_time"]): feats(r) for r in ROWS}
KEYS = list(next(iter(FE.values())).keys())


def _auc(pos, neg):
    """P(pos > neg) — Mann-Whitney AUC. 0.5 = 무정보."""
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    a = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
    return a / (len(pos) * len(neg))


def compare(tag, is_give):
    G = [r for r in ROWS if is_give(r)]
    Kp = [r for r in ROWS if not is_give(r)]
    print(f"\n[{tag}]  KEEPER {len(Kp)}건 vs GIVEBACK {len(G)}건")
    print(f"  {'feature':14s} {'KEEPER평균':>10s} {'GIVE평균':>10s} {'KEEPER중앙':>10s} "
          f"{'GIVE중앙':>9s} {'AUC(GIVE↑)':>10s} {'판별력':>6s}")
    print("  " + "-" * 78)
    rows = []
    for k in KEYS:
        gv = [FE[(r["date"], r["entry_time"])][k] for r in G]
        kv = [FE[(r["date"], r["entry_time"])][k] for r in Kp]
        auc = _auc(gv, kv)
        rows.append((abs(auc - 0.5) if auc == auc else -1, k, kv, gv, auc))
    for _, k, kv, gv, auc in sorted(rows, reverse=True):
        kk = [x for x in kv if x == x]; gg = [x for x in gv if x == x]
        print(f"  {k:14s} {np.mean(kk):10.3f} {np.mean(gg):10.3f} {np.median(kk):10.3f} "
              f"{np.median(gg):9.3f} {auc:10.3f} {'*' * int(min(4, max(0, (abs(auc-0.5)-0.05)//0.05))):>6s}")


if __name__ == "__main__":
    print("=" * 120)
    print(f"[G section2] 도달 시점 feature 비교 — 30영업일 {W30[0]}~{W30[-1]}, 대상 {len(ROWS)}건")
    print("=" * 120)
    compare("정의1  최종 < +1.0%", cls_def1)
    compare("정의2  MFE 대비 1.0%p+ 반납", cls_def2)
    compare("정의C  SEVERE (최종 <= 0)", lambda r: r["net_pct"] <= 0.0)
