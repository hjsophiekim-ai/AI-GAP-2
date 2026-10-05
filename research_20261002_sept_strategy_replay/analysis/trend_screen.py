"""R0(P3 production, 83일 183거래)에서 '역추세 진입' 정의별 성과 (removal-only 스크리닝). READ-ONLY."""
import pickle, sys
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
C = r"C:/Users/FURSYS/Desktop/AI-GAP 2/research_20260930_p3_studies/engine/cache84"
t = pickle.load(open(S + "/research_20260930_highvol_day_mode/lab/out_H30_d83.pkl", "rb"))["trades"]
hy = {}
def H(d):
    if d not in hy:
        x = pd.read_csv(f"{C}/replay_{d}_hynix_1m.csv", parse_dates=["datetime"])
        x["datetime"] = x["datetime"].dt.tz_localize(None) if x["datetime"].dt.tz is not None else x["datetime"]; hy[d] = x[x["datetime"].dt.strftime("%Y%m%d") == d].reset_index(drop=True)
    return hy[d]
rows = []
for x in t:
    d = x["date"]; et = pd.Timestamp(x["entry_time"]).tz_localize(None)
    h = H(d); reg = h[h["datetime"] >= pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 09:00")]
    seen = reg[reg["datetime"] + pd.Timedelta(minutes=1) <= et]
    if len(seen) == 0: continue
    op = float(reg["open"].iloc[0]); last = float(seen["close"].iloc[-1])
    tp = (seen["high"] + seen["low"] + seen["close"]) / 3; vw = float((tp * seen["volume"]).sum() / max(seen["volume"].sum(), 1))
    m30 = reg[reg["datetime"] < pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 09:30")]
    up = x["direction"] == "UP_RED"; s = 1 if up else -1
    f = dict(date=d, t=et.strftime("%H:%M"), up=up, net=x["net_pct"], peak=x["peak_net_pct"],
             open_ok=(last - op) * s > 0, vwap_ok=(last - vw) * s > 0, ema_ok=bool(x["trend_at_entry"]),
             slope_ok=(x["e_slope"] or 0) * s > 0,
             m30_ok=None if et.hour * 60 + et.minute < 9 * 60 + 30 else (float(m30["close"].iloc[-1]) - op) * s > 0,
             am=et.hour < 12)
    rows.append(f)
df = pd.DataFrame(rows)
print("trades", len(df), "sum net", round(df.net.sum(), 2))
def rep(name, mask, sub):
    for lab, part in (("83일", sub), ("9월", sub[sub.date >= "20260901"]), ("주간", sub[sub.date >= "20260922"])):
        m = mask.loc[part.index]
        a, b = part[m], part[~m]
        print(f"  {name:10s} {lab}: 순추세 n={len(a):3d} 합={a.net.sum():7.2f} 평균={a.net.mean() if len(a) else 0:5.2f} | 역추세 n={len(b):3d} 합={b.net.sum():7.2f} 평균={b.net.mean() if len(b) else 0:5.2f} 승률={(b.net>0).mean() if len(b) else 0:.2f}")
for k in ("open_ok", "vwap_ok", "ema_ok", "slope_ok"):
    rep(k, df[k].astype(bool), df)
    rep(k + "/오전", df[k].astype(bool), df[df.am])
sub = df[df.m30_ok.notna() & df.am]
rep("m30/오전", sub.m30_ok.astype(bool), sub)
df.to_pickle(S + "/wk/lab/trend_feat.pkl")
print(df[df.date >= "20260922"].to_string())
