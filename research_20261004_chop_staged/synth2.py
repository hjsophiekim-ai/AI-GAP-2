"""거래별 경로(MFE/MAE) 진단 — 러너와 손절을 진입 직후에 구분할 수 있는가.
기존 A 재생 결과 + ETF 1분봉만 사용. 재생 불필요. READ-ONLY.

주의: 1분봉 고가/저가 기준 경로 진단은 엔진(틱=20초 보간) 결과와 다른 집합을 만들 수 있다.
여기서는 **진단 전용**이며, 어떤 후보든 채택 전 전체 replay 검증이 필요하다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
DATA = REPO + "/research_20261002_sept_strategy_replay/data"
KST = "Asia/Seoul"
LONG, INV = "0193T0", "0197X0"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

_c = {}


def etf(day, sym):
    k = (day, sym)
    if k not in _c:
        tag = "long" if sym == LONG else "inverse"
        d = pd.read_csv(f"{DATA}/replay_{day}_{tag}_1m.csv")
        d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
        _c[k] = d.sort_values("datetime").reset_index(drop=True)
    return _c[k]


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for d in DAYS:
    for t in trades(json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8"))):
        sym = LONG if t["dir"].startswith("UP") else INV
        b = etf(d, sym)
        w = b[(b["datetime"] >= t["entry"].floor("min")) & (b["datetime"] <= t["exit"])]
        if len(w) < 2:
            continue
        p = float(t["px_in"])
        mfe = (w["high"].max() - p) / p * 100
        mae = (w["low"].min() - p) / p * 100
        r = dict(day=d, entry=t["entry"], rs="+".join(dict.fromkeys(t["reasons"])),
                 krw=t["krw"], net=t["net"], mfe=mfe, mae=mae,
                 hold=t["hold_min"])
        # 진입 후 N분 시점의 미실현 수익률(종가 기준)
        for n in (3, 5, 10, 15):
            ww = w[w["datetime"] <= t["entry"] + pd.Timedelta(minutes=n)]
            r[f"r{n}"] = (float(ww["close"].iloc[-1]) - p) / p * 100 if len(ww) else np.nan
            r[f"mae{n}"] = (float(ww["low"].min()) - p) / p * 100 if len(ww) else np.nan
            r[f"mfe{n}"] = (float(ww["high"].max()) - p) / p * 100 if len(ww) else np.nan
        rows.append(r)
df = pd.DataFrame(rows)
df["bucket"] = np.where(df.rs.str.contains("TP2_FULL"), "RUNNER(TP2)",
                np.where(df.rs.str.contains("STOP_LOSS"), "N1손절",
                np.where(df.rs == "B3_SL", "B3손절", "기타")))
print(f"# {len(df)}거래 경로 산출 (A 85일)")
print("\n## 버킷별 경로")
g = df.groupby("bucket").agg(건수=("krw", "size"), 손익=("krw", "sum"), MFE=("mfe", "mean"),
                             MAE=("mae", "mean"), r5=("r5", "mean"), r10=("r10", "mean"),
                             mae5=("mae5", "mean"), mfe5=("mfe5", "mean"), 보유=("hold", "mean"))
print(g.round(2).to_string())

print("\n## 진입 후 5분 시점으로 러너를 구분할 수 있는가")
run = df[df.bucket == "RUNNER(TP2)"]
sl = df[df.bucket.isin(["N1손절", "B3손절"])]
for col in ("r5", "mfe5", "mae5", "r10", "mfe10", "mae10"):
    print(f"  {col:6s}  러너 {run[col].mean():+.3f} (중앙 {run[col].median():+.3f})"
          f"   손절 {sl[col].mean():+.3f} (중앙 {sl[col].median():+.3f})")

print("\n## '진입 후 N분까지 MAE가 -X% 이하면 즉시 청산' 상한 (경로 기준 진단)")
print("| 규칙 | 발동 | 그중 러너 | 러너손익 | 손절거래 | 손절거래 손익 |")
print("|---|---|---|---|---|---|")
for n in (5, 10):
    for x in (-0.8, -1.0):
        m = df[f"mae{n}"] <= x
        z = df[m]
        print(f"| {n}분내 MAE<={x}% | {int(m.sum())}건 | {int((z.bucket=='RUNNER(TP2)').sum())}건 | "
              f"{z[z.bucket=='RUNNER(TP2)']['krw'].sum():+,.0f} | "
              f"{int(z.bucket.isin(['N1손절','B3손절']).sum())}건 | "
              f"{z[z.bucket.isin(['N1손절','B3손절'])]['krw'].sum():+,.0f} |")

print("\n## N1 손절거래 57건의 MFE 분포 (손절 전에 얼마나 올라갔었나)")
n1 = df[df.bucket == "N1손절"]
print(n1["mfe"].describe(percentiles=[.25, .5, .75, .9]).round(2).to_string())
print(f"  MFE < +0.5% 인 손절: {int((n1.mfe < 0.5).sum())}건 {n1[n1.mfe<0.5]['krw'].sum():+,.0f}원")
print(f"  MFE >= +1.0% 인 손절: {int((n1.mfe >= 1.0).sum())}건 {n1[n1.mfe>=1.0]['krw'].sum():+,.0f}원")

print("\n## 러너 22건의 MAE 분포 (러너는 얼마나 역행했었나)")
print(run["mae"].describe(percentiles=[.1, .25, .5]).round(2).to_string())
df.to_csv(ROOT + "/synth2_paths.csv", index=False, encoding="utf-8")
