# -*- coding: utf-8 -*-
"""[P1] confirmation-path feature — 플래그 -> T+3 승인 -> 진입 직전까지의 정보만.

시간 구조 (N1_SPEC / 실원장 대조로 확인됨)
    플래그봉 p (3분)  ->  T+3 판정봉 k = p+1 (3분)  ->  진입 1분봉 = entry_time
    예: 09:51 플래그 -> 09:54 봉에서 승인 -> 09:57 진입

미래정보 차단
    * 하이닉스 3분봉은 index <= k (= last_done(entry_time)) 만
    * 1분봉(하이닉스/ETF)은 **datetime < entry_time** 만 (진입봉 자체 제외)
      entry_price 가 진입봉 종가이므로 그 봉을 쓰면 선취가 된다.
READ-ONLY.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
import g6_entry as E

A, TS, D, W30, OOS = E.A, E.TS, E.D, E.W30, E.OOS
B3, HIST, CL = E.B3, E.HIST, E.CL
CACHE = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2") / "data" / "cache"
KST = "Asia/Seoul"

# ── 하이닉스 1분봉 ─────────────────────────────────────────────────────
_h = []
for f in sorted(CACHE.glob("replay_*_hynix_1m.csv")):
    s = f.stem.split("_")[1]
    if not (s.isdigit() and len(s) == 8):
        continue
    d = pd.read_csv(f)
    d["datetime"] = pd.to_datetime(d["datetime"], format="ISO8601")
    d["datetime"] = (d["datetime"].dt.tz_localize(KST) if d["datetime"].dt.tz is None
                     else d["datetime"].dt.tz_convert(KST))
    _h.append(d)
H1 = (pd.concat(_h, ignore_index=True).drop_duplicates("datetime", keep="last")
      .sort_values("datetime").reset_index(drop=True))
H1T = pd.DatetimeIndex(H1["datetime"])
ETF = {s: (P.bars(s), pd.DatetimeIndex(P.bars(s)["datetime"])) for s in ("0193T0", "0197X0")}


def confirm_feats(t) -> dict:
    k = E.last_done(t["entry_time"])                 # T+3 판정봉
    p = k - 1                                        # 플래그봉
    up = t["direction"] == "UP_RED"
    sgn = 1.0 if up else -1.0
    ent = pd.Timestamp(t["entry_time"])
    f = {}
    if k < 4:
        return {}

    # ── MACD gap (하이닉스 3분 완성봉) ──────────────────────────────
    g0, g1, g2 = sgn * HIST[k], sgn * HIST[k - 1], sgn * HIST[k - 2]
    f["gap_3연속확대"] = 1.0 if (g0 > g1 > g2) else 0.0
    f["gap_증가율"] = (g0 - g1) / max(abs(g1), 1e-9)
    f["gap_가속도"] = (g0 - g1) - (g1 - g2)
    f["gap_절대"] = g0

    # ── confirmation 구간 1분봉 (플래그봉 시작 ~ 진입 직전) ──────────
    t0 = pd.Timestamp(B3["datetime"].iloc[p])
    hm = H1[(H1T >= t0) & (H1T < ent)]
    eb, et = ETF[t["symbol"]]
    em = eb[(et >= t0) & (et < ent)]
    if len(hm) < 3 or len(em) < 3:
        return {}
    ho, hc = float(hm["close"].iloc[0]), float(hm["close"].iloc[-1])
    eo, ec = float(em["close"].iloc[0]), float(em["close"].iloc[-1])
    hmove = sgn * (hc - ho) / ho * 100.0            # 신호 방향으로 실제 이동한 %
    emove = (ec - eo) / eo * 100.0                  # ETF 는 보유방향이 곧 상승
    f["하이닉스_방향이동%"] = hmove
    f["ETF_추종%"] = emove
    f["괴리(ETF-2x하이닉스)"] = emove - 2.0 * hmove  # 레버리지 2배 기준 정규화
    f["추종비율"] = emove / hmove if abs(hmove) > 0.02 else np.nan

    # ── 최근 5분 상관 ───────────────────────────────────────────────
    hr = np.diff(np.log(pd.to_numeric(hm["close"], errors="coerce").to_numpy()))[-5:]
    er = np.diff(np.log(pd.to_numeric(em["close"], errors="coerce").to_numpy()))[-5:]
    n = min(len(hr), len(er))
    f["corr_5분"] = (float(np.corrcoef(hr[-n:], er[-n:])[0, 1])
                    if n >= 3 and np.std(hr[-n:]) > 0 and np.std(er[-n:]) > 0 else np.nan)

    # ── 거래량 ──────────────────────────────────────────────────────
    prior = H1[(H1T < t0)].tail(20)
    pv = float(pd.to_numeric(prior["volume"], errors="coerce").mean()) if len(prior) >= 5 else np.nan
    f["하이닉스_거래량배"] = (float(pd.to_numeric(hm["volume"], errors="coerce").mean()) / pv
                        if pv and pv > 0 else np.nan)
    epr = eb[(et < t0)].tail(20)
    epv = float(pd.to_numeric(epr["volume"], errors="coerce").mean()) if len(epr) >= 5 else np.nan
    f["ETF_거래량배"] = (float(pd.to_numeric(em["volume"], errors="coerce").mean()) / epv
                     if epv and epv > 0 else np.nan)

    # ── 역행봉 / 고저 갱신 ──────────────────────────────────────────
    ecl = pd.to_numeric(em["close"], errors="coerce").to_numpy()
    f["ETF_역행봉수"] = float(sum(1 for i in range(1, len(ecl)) if ecl[i] < ecl[i - 1]))
    f["ETF_역행비율"] = f["ETF_역행봉수"] / max(1, len(ecl) - 1)
    hcl = pd.to_numeric(hm["close"], errors="coerce").to_numpy()
    f["하이닉스_역행봉수"] = float(sum(1 for i in range(1, len(hcl))
                               if sgn * (hcl[i] - hcl[i - 1]) < 0))
    # 판정봉이 플래그봉의 고(저)를 갱신했는가
    fb = hm[H1T[(H1T >= t0) & (H1T < ent)] < pd.Timestamp(B3["datetime"].iloc[k])]
    cb = hm[H1T[(H1T >= t0) & (H1T < ent)] >= pd.Timestamp(B3["datetime"].iloc[k])]
    if len(fb) and len(cb):
        if up:
            f["고점갱신"] = 1.0 if float(cb["high"].max()) > float(fb["high"].max()) else 0.0
        else:
            f["고점갱신"] = 1.0 if float(cb["low"].min()) < float(fb["low"].min()) else 0.0
    else:
        f["고점갱신"] = np.nan
    f["플래그→진입_분"] = float((ent - t0).total_seconds() // 60)
    return f


FE = {}
for t in A:
    v = confirm_feats(t)
    if v:
        FE[(t["date"], t["entry_time"])] = v
KEYS = list(next(iter(FE.values())).keys())


def auc(pos, neg):
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    return sum((1.0 if a > b else 0.5 if a == b else 0.0)
               for a in pos for b in neg) / (len(pos) * len(neg))


if __name__ == "__main__":
    print("=" * 118)
    print("[P1] confirmation-path feature 의 B(MFE<1.5%) 판별력")
    print("=" * 118)
    print(f"  feature 계산 성공 {len(FE)}/{len(A)}건")
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        SW = set(W)
        na = sum(1 for t in A if t["date"] in SW and t["mfe"] >= 1.5)
        nb = sum(1 for t in A if t["date"] in SW and t["mfe"] < 1.5)
        print(f"  {tag:10s} A(>=1.5%) {na:3d} / B(<1.5%) {nb:3d}")
    print()
    print(f"  {'feature':22s} {'AUC 78일':>9s} {'AUC 30일':>9s} {'AUC OOS48':>10s} "
          f"{'A 평균':>9s} {'B 평균':>9s} {'|Δ|78':>6s}")
    print("  " + "-" * 82)
    rows = []
    for k in KEYS:
        cells = []
        for W in (D, W30, OOS):
            SW = set(W)
            bv = [FE[(t["date"], t["entry_time"])][k] for t in A
                  if t["date"] in SW and t["mfe"] < 1.5 and (t["date"], t["entry_time"]) in FE]
            av = [FE[(t["date"], t["entry_time"])][k] for t in A
                  if t["date"] in SW and t["mfe"] >= 1.5 and (t["date"], t["entry_time"]) in FE]
            cells.append(auc(bv, av))
        av = [FE[(t["date"], t["entry_time"])][k] for t in A
              if (t["date"], t["entry_time"]) in FE and t["mfe"] >= 1.5]
        bv = [FE[(t["date"], t["entry_time"])][k] for t in A
              if (t["date"], t["entry_time"]) in FE and t["mfe"] < 1.5]
        rows.append((abs(cells[0] - 0.5) if cells[0] == cells[0] else -1, k, cells, av, bv))
    for d78, k, c, av, bv in sorted(rows, reverse=True):
        flip = "" if (c[1] - 0.5) * (c[2] - 0.5) > 0 else "  <= 창끼리 방향 반대"
        print(f"  {k:22s} {c[0]:9.3f} {c[1]:9.3f} {c[2]:10.3f} "
              f"{np.nanmean(av):9.3f} {np.nanmean(bv):9.3f} {d78:6.3f}{flip}")

    print("\n  [대조] 정적 entry feature (g6) 의 같은 판별력")
    print(f"  {'feature':22s} {'AUC 78일':>9s} {'AUC 30일':>9s} {'AUC OOS48':>10s} {'|Δ|78':>6s}")
    print("  " + "-" * 62)
    st = []
    for k in list(next(iter(E.FE.values())).keys()):
        cells = []
        for W in (D, W30, OOS):
            SW = set(W)
            bv = [E.FE[(t["date"], t["entry_time"])][k] for t in A if t["date"] in SW and t["mfe"] < 1.5]
            av = [E.FE[(t["date"], t["entry_time"])][k] for t in A if t["date"] in SW and t["mfe"] >= 1.5]
            cells.append(auc(bv, av))
        st.append((abs(cells[0] - 0.5) if cells[0] == cells[0] else -1, k, cells))
    for d78, k, c in sorted(st, reverse=True)[:6]:
        print(f"  {k:22s} {c[0]:9.3f} {c[1]:9.3f} {c[2]:10.3f} {d78:6.3f}")
