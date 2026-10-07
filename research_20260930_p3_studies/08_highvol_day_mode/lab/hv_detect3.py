"""D3 출렁임 탐지기 (인과적, 매 시점 재판정). 하이닉스 1분봉, 정규장 09:00~.
  ER(t) = |close_t - open_0900| / Σ|Δclose| (09:00~t)  < 0.3  (Kaufman 효율비 표준 기준)
  AND  mean|1분 수익률|(09:00~t) >= 직전 20영업일 같은 시각 값의 중앙값
  09:30 부터 판정, t 분봉이 끝난 시각(t+1분)부터 사용. 사전 등록 값: 0.3 / 중앙값 / 20일 / 09:30."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
C, ER = {}, {}
for f in sorted((HERE / "cache84").glob("replay_*_hynix_1m.csv")):
    d = f.name.split("_")[1]
    x = pd.read_csv(f, parse_dates=["datetime"]).set_index("datetime").between_time("09:00", "15:19")
    if len(x) < 60: continue
    cl = x["close"]; o = float(x["open"].iloc[0])
    r = cl.pct_change().abs() * 100
    path = cl.diff().abs().cumsum()
    er = (cl - o).abs() / path.replace(0, np.nan)
    idx = (x.index + pd.Timedelta(minutes=1)).strftime("%H:%M")
    C[d] = pd.Series(r.expanding().mean().values, index=idx); ER[d] = pd.Series(er.values, index=idx)
MODE = sys.argv[1] if len(sys.argv) > 1 else "a"
days = sorted(C); act = {}; summ = []
for i, d in enumerate(days):
    prior = days[max(0, i - 20):i]
    if len(prior) < 10: continue
    med = pd.DataFrame({p: C[p] for p in prior}).median(axis=1)
    emed = pd.DataFrame({p: ER[p] for p in prior}).median(axis=1)
    ermask = (ER[d] < 0.3) if MODE == "a" else (ER[d] <= emed.reindex(ER[d].index))
    ok = ermask & (C[d] >= med.reindex(C[d].index)) & (C[d].index >= "09:30")
    s = set(ok[ok].index)
    if s: act[d] = s
    summ.append((d, len(s), min(s) if s else "-", round(float(ER[d].get("10:00", np.nan)), 2), round(float(ER[d].iloc[-1]), 2)))
pickle.dump(act, open(HERE / f"hv_on3{MODE}.pkl", "wb"))
CD = list(pickle.load(open(HERE / "_ctx84.pkl", "rb"))["dates"])
W80 = CD[-81:-1]
print("최근 80영업일: 출렁임 활성 있었던 날", sum(d in act for d in W80), "/ 80")
for d, n, first, e10, eend in summ:
    if d >= "20260901": print(d, f"활성 {n:3d}분", "첫", first, f"| ER 10:00 {e10} / 종가 {eend}")
