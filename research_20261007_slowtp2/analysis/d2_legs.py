"""D2: 플래그 레그(flag -> 다음 반대플래그) 진폭. READ-ONLY.

핵심 질문: 플래그 시점에 **이미 관측 가능한** 직전 반대레그의 크기가,
그 플래그가 만들 다음 레그의 크기를 예측하는가?
예측한다면 "오늘처럼 크게 왔다갔다 한 날은 더 길게 들고 간다"가 성립한다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
DATA = REPO + "/research_20261004_chop_staged/wk/data"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def m1(day):
    f = f"{DATA}/replay_{day}_hynix_1m.csv"
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    return df.sort_values("datetime").reset_index(drop=True)


rows = []
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")} | {"20261002"})
for day in DAYS:
    df = m1(day)
    if df is None:
        continue
    o = json.load(open(apath(day), encoding="utf-8"))
    # 장중 플래그만 (09:00 이후). 방향 교대만 남긴다 = 레그 경계.
    fl = []
    for s in o["sig"]:
        t = pd.Timestamp(s["detected_at"])
        if t.hour < 9 or (t.hour, t.minute) >= (15, 20):
            continue
        if fl and fl[-1][1] == s["direction"]:
            continue                       # 같은 방향 연속은 레그를 끊지 않는다
        fl.append((t, s["direction"]))
    ts = pd.DatetimeIndex(df["datetime"])
    hi = df["high"].to_numpy(float); lo = df["low"].to_numpy(float); cl = df["close"].to_numpy(float)
    for k, (t, d) in enumerate(fl):
        i0 = int(ts.searchsorted(t))
        if i0 >= len(ts):
            continue
        p0 = cl[i0]
        # 레그 끝 = 다음 반대플래그 시각 (없으면 15:20)
        tend = fl[k + 1][0] if k + 1 < len(fl) else pd.Timestamp(f"{day} 15:20", tz=KST)
        i1 = int(ts.searchsorted(tend))
        if i1 <= i0:
            continue
        seg_hi, seg_lo = hi[i0:i1].max(), lo[i0:i1].min()
        up = d == "UP_RED"
        mfe = ((seg_hi - p0) if up else (p0 - seg_lo)) / p0 * 100      # 순방향 최대진행
        mae = ((p0 - seg_lo) if up else (seg_hi - p0)) / p0 * 100      # 역방향 최대역행
        rows.append(dict(day=day, k=k, at=t.strftime("%H:%M"),
                         mins=t.hour * 60 + t.minute, dir=d,
                         dur=(tend - t).total_seconds() / 60,
                         mfe=mfe, mae=mae))

L = pd.DataFrame(rows)
L["prev_mfe"] = L.groupby("day")["mfe"].shift(1)
L["prev_dur"] = L.groupby("day")["dur"].shift(1)
L.to_csv(OUT + "/legs.csv", index=False, encoding="utf-8")
print(f"# 레그 {len(L)}건 / {L.day.nunique()}일  (직전레그 있는 건 {L.prev_mfe.notna().sum()})")
print("\n## 레그 순방향 최대진행(MFE %) 분위수")
print(L.groupby(L.mins < 11 * 60 + 30)["mfe"].describe(percentiles=[.25, .5, .75, .9]).round(2)
      .rename(index={True: "오전(<11:30)", False: "오후"}).to_string())

S = L.dropna(subset=["prev_mfe"]).copy()
print(f"\n## 직전레그 MFE -> 현재레그 MFE  (n={len(S)})")
print(f"   피어슨 r = {S.prev_mfe.corr(S.mfe):.3f} / 스피어만 = {S.prev_mfe.rank().corr(S.mfe.rank()):.3f}")
S["q"] = pd.qcut(S.prev_mfe, 4, labels=["Q1 작음", "Q2", "Q3", "Q4 큼"])
print(S.groupby("q", observed=True).agg(n=("mfe", "size"), 현재MFE=("mfe", "mean"),
                                        중앙값=("mfe", "median"), 지속분=("dur", "mean")).round(2).to_string())
print("\n## 임계별 (직전레그 MFE >= x)")
print("| 임계 | n | 현재레그 MFE 평균 | 중앙값 | MFE>=1.0% 비율 | 미달 n | 미달 MFE 평균 |")
print("|---|---|---|---|---|---|---|")
for x in (0.6, 0.8, 1.0, 1.2, 1.5, 2.0):
    a, b = S[S.prev_mfe >= x], S[S.prev_mfe < x]
    if len(a) < 5:
        continue
    print(f"| >={x:.1f}% | {len(a)} | {a.mfe.mean():.2f}% | {a.mfe.median():.2f}% | "
          f"{(a.mfe >= 1.0).mean()*100:.0f}% | {len(b)} | {b.mfe.mean():.2f}% |")
