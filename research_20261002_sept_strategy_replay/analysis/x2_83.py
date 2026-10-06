"""주간 1위 청산안(X2 반대확인플래그까지 / X4 +손절1%)을 83일 R0 진입에 적용한 교차검증. READ-ONLY.
진입 = 엔진 R0 진입시각/진입가 (R0 와 같은 가정이라 상대비교는 공정). 반대 확인 플래그 = 3분 MACD hist 교차 후 2봉 유지 근사.
"""
import sys
import numpy as np
import pandas as pd
import importlib.util
S = sys.argv[1]
spec = importlib.util.spec_from_file_location("dt", S + "/wk/lab/daytrend_core.py")
sys.argv = [sys.argv[0], S]
dt = importlib.util.module_from_spec(spec); spec.loader.exec_module(dt)
import pickle
R0 = pickle.load(open(S + "/research_20260930_highvol_day_mode/lab/out_H30_d83.pkl", "rb"))["trades"]
FEE = dt.FEE
RULES = {"X2 반대확인까지": dict(), "X4 X2+손절1%": dict(sl=1.0), "X3 X2+손절2%": dict(sl=2.0)}
out = {k: {} for k in RULES}
r0d = {}
for t in R0:
    r0d[t["date"]] = r0d.get(t["date"], 0.0) + t["net_pct"]
bydate = {}
for t in R0:
    bydate.setdefault(t["date"], []).append(t)
for d, ts in bydate.items():
    if d not in dt.FL:
        x = dt.day_frame(d); dt.FL[d] = (x, dt.confirmed_flags(x) if x is not None else [])
    x, fl = dt.FL[d]
    end = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00")
    ts = sorted(ts, key=lambda z: z["entry_time"])
    for name, rule in RULES.items():
        held_until, held_dir, s = None, None, 0.0
        for t in ts:
            et = pd.Timestamp(t["entry_time"]).tz_localize(None)
            f = 1 if t["direction"] == "UP_RED" else -1
            if held_until is not None and et < held_until and f == held_dir:
                continue
            e = dt.etf(d, f)
            p_in = float(t["entry_price"])
            opp = [o[0] for o in fl if o[0] > et and o[1] != f]
            xt, net = None, None
            for r in e[e["datetime"] + pd.Timedelta(minutes=1) > et].itertuples():
                tt = r.datetime + pd.Timedelta(minutes=1)
                ret = (r.close / p_in - 1) * 100
                if (rule.get("sl") and ret <= -rule["sl"]) or any(o <= tt for o in opp) or tt >= end:
                    xt, net = tt, ret - FEE; break
            if xt is None:
                r = e.iloc[-1]; xt, net = r["datetime"], (r["close"] / p_in - 1) * 100 - FEE
            held_until, held_dir = xt, f
            s += net
        out[name][d] = s
days = sorted(r0d)
for scope, ds in (("83일", days), ("0527~0731", [d for d in days if d < "20260801"]), ("8월", [d for d in days if "20260801" <= d < "20260901"]),
                  ("9월", [d for d in days if d >= "20260901"]), ("주간(0922~0929)", [d for d in days if d >= "20260922"])):
    print(f"[{scope}] {len(ds)}일   R0 " + dt.stats([r0d.get(d, 0) for d in ds]))
    for name in RULES:
        v = [out[name].get(d, 0) for d in ds]
        diff = np.array(v) - np.array([r0d.get(d, 0) for d in ds])
        print(f"   {name:14s} " + dt.stats(v) + f"   R0 대비 일별 우위 {(diff > 0).sum()}/{len(ds)}")
