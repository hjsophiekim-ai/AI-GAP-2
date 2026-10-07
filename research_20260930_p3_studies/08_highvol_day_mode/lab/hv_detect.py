"""HIGH-VOL 일 탐지기 (인과적). 하이닉스 1분 |수익률| 의 09:00~t 누적평균이
직전 20영업일 같은 시각 누적평균의 80백분위를 처음 넘는 시각(09:15 이후)부터 그날 끝까지 ON.
사전 등록 값: 창 20일, 80백분위, 시작 09:15. (탐색하지 않음)"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
files = sorted((HERE / "cache84").glob("replay_*_hynix_1m.csv"))
C = {}
for f in files:
    d = f.name.split("_")[1]
    x = pd.read_csv(f, parse_dates=["datetime"]).set_index("datetime").between_time("09:00", "15:19")
    if len(x) < 60: continue
    r = x["close"].pct_change().abs() * 100
    c = r.expanding().mean()
    c.index = c.index.strftime("%H:%M")
    C[d] = c
days = sorted(C)
on = {}
rows = []
for i, d in enumerate(days):
    prior = days[max(0, i - 20):i]
    if len(prior) < 10: continue
    M = pd.DataFrame({p: C[p] for p in prior})
    thr = M.quantile(0.80, axis=1)
    c = C[d]
    hit = [(t, v, thr.get(t)) for t, v in c.items() if t >= "09:15" and t in thr.index and pd.notna(thr[t]) and v > thr[t]]
    if hit:
        on[d] = hit[0][0]
    rows.append((d, hit[0][0] if hit else "-", round(float(c.get("10:00", np.nan)), 4), round(float(thr.get("10:00", np.nan)), 4)))
pickle.dump(on, open(HERE / "hv_on.pkl", "wb"))
print("평가 가능일", len(rows), "HIGH-VOL 일", len(on))
for d, t, v, th in rows:
    if d >= "20260601": print(d, "ON " + t if t != "-" else "   -", f"10:00 누적 {v} vs 기준 {th}")
