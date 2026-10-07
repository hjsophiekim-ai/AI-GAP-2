"""R2 — 항목1 detector 민감도 + 항목2 regime 내부 거래특성. 엔진 재실행 없음.

포지션이 항상 단일이라 trades 의 순서 = 시간순이고, 거래 i 진입 시점에 청산된
거래는 정확히 trades[:i] 다. 엔진의 trades[-K:] 와 동일한 지연계산을 재현한다.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 300); pd.set_option("display.max_columns", 50)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "r1.pkl", "rb"))
d = pd.DataFrame(Z["base"]).sort_values("exit_time").reset_index(drop=True)
d["mfe"] = d.peak_net_pct; d["mae"] = d.mae_net_pct
d["pnl"] = d.net_pct * d.w1a; d["win"] = d.net_pct > 0
d["ym"] = d.date.astype(str).str[:6]
d["is_sep"] = d.date.astype(str) >= "20260901"
t = pd.to_datetime(d.entry_time, utc=True).dt.tz_convert("Asia/Seoul")
d["tmin"] = t.dt.hour * 60 + t.dt.minute
h50 = d.h50_held.astype(float).values
tp1 = d.tp1_hit.astype(float).values


def detect(K, hth, qth):
    on = np.zeros(len(d), bool); hr = np.full(len(d), np.nan); qr = np.full(len(d), np.nan)
    for i in range(len(d)):
        if i < K:
            continue
        h, q = h50[i - K:i].mean(), tp1[i - K:i].mean()
        hr[i], qr[i] = h, q
        on[i] = (h >= hth) and (q <= qth)
    return on, hr, qr


def comp(p):
    p = np.asarray(p, float)
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("=" * 118); print("1. DETECTOR 민감도 — plateau 확인 (최적점 탐색 아님)"); print("=" * 118)
rows = []
for K in (8, 10, 12):
    for hth in (0.30, 0.40, 0.50):
        for qth in (0.15, 0.20, 0.25):
            on, hr, qr = detect(K, hth, qth)
            s, n = on[d.is_sep.values], on[~d.is_sep.values]
            sub = d[on]
            rows.append(dict(K=K, H50=hth, TP1=qth,
                             탐지_9월=round(100 * s.mean(), 1) if len(s) else 0,
                             오탐_비9월=round(100 * n.mean(), 1) if len(n) else 0,
                             ON거래=int(on.sum()),
                             ON평균H50=round(float(np.nanmean(hr[on])), 3) if on.any() else np.nan,
                             ON평균TP1=round(float(np.nanmean(qr[on])), 3) if on.any() else np.nan,
                             ON_MFE평균=round(sub.mfe.mean(), 3) if len(sub) else np.nan,
                             ON_MFE3이상=round(100 * (sub.mfe >= 3).mean(), 1) if len(sub) else np.nan))
S = pd.DataFrame(rows)
print(S.to_string(index=False))
print("\n[비교] 전체 MFE평균 %.3f · MFE>=3%% 비율 %.1f%%"
      % (d.mfe.mean(), 100 * (d.mfe >= 3).mean()))

print("\n[월별 탐지율]")
for K, hth, qth in ((10, 0.40, 0.20),):
    on, hr, qr = detect(K, hth, qth)
    t2 = d.assign(on=on).groupby("ym").agg(거래=("on", "size"), 탐지=("on", "sum"))
    t2["탐지율%"] = (100 * t2.탐지 / t2.거래).round(1)
    print("  K=%d H50>=%.2f TP1<=%.2f" % (K, hth, qth)); print(t2.to_string())

# ── 항목 2 ────────────────────────────────────────────────────────────────
K, HTH, QTH = 10, 0.40, 0.20
on, hr, qr = detect(K, HTH, QTH)
d["on"] = on
print("\n" + "=" * 118)
print("2. REGIME 내부 거래 특성 — detector ON/OFF (K=%d H50>=%.2f TP1<=%.2f)" % (K, HTH, QTH))
print("=" * 118)
rows = []
for lab, g in (("ON", d[d.on]), ("OFF", d[~d.on]), ("전체", d)):
    if not len(g):
        continue
    w = g[g.net_pct > 0]; l = g[g.net_pct <= 0]
    eq = np.cumprod(1 + g.sort_values("exit_time").pnl.values / 100)
    rows.append(dict(구분=lab, 거래=len(g), 승률=round(100 * g.win.mean(), 1),
                     평균net=round(g.net_pct.mean(), 3),
                     PF=round(w.pnl.sum() / -l.pnl.sum(), 3) if len(l) and l.pnl.sum() else np.inf,
                     복리=round(comp(g.sort_values("exit_time").pnl), 2),
                     MDD기여=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     MFE평균=round(g.mfe.mean(), 3), MAE평균=round(g.mae.mean(), 3),
                     MFE15=round(100 * (g.mfe >= 1.5).mean(), 1),
                     MFE25=round(100 * (g.mfe >= 2.5).mean(), 1),
                     MFE3=round(100 * (g.mfe >= 3).mean(), 1),
                     MFE5=round(100 * (g.mfe >= 5).mean(), 1),
                     TP1율=round(100 * g.tp1_hit.mean(), 1),
                     C1발동=round(100 * g.pp_fired.mean(), 1),
                     H50율=round(100 * g.h50_held.mean(), 1),
                     RED=int((g.direction == "UP_RED").sum()),
                     BLUE=int((g.direction == "DOWN_BLUE").sum())))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[slot 별]")
print(d.groupby(["on", "slot_number"]).agg(거래=("net_pct", "size"), 승률=("win", "mean"),
                                           MFE=("mfe", "mean"), net=("net_pct", "mean")).round(3).to_string())
print("\n[시간대별]")
TE = [0, 600, 690, 780, 840, 1440]; TL = ["09:00~10:00", "10:00~11:30", "11:30~13:00", "13:00~14:00", "14:00~"]
d["tb"] = pd.cut(d.tmin, TE, labels=TL, right=False)
print(d.groupby(["on", "tb"], observed=True).agg(거래=("net_pct", "size"), 승률=("win", "mean"),
                                                 MFE=("mfe", "mean"), net=("net_pct", "mean")).round(3).to_string())
pickle.dump({"on": on, "K": K, "HTH": HTH, "QTH": QTH}, open(HERE / "r2.pkl", "wb"))
print("\nsaved r2.pkl")
