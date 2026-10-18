"""확정플래그 전수 × 시간대 × 사전 판별력 (2026-10-18). READ-ONLY.

지금까지의 feature 분석은 **체결된 162건**만 봤다. 여기서는 **확정플래그 전부**
(:TW2_3SLOT_CONFIRM, 체결/차단 무관)를 대상으로 한다 — "들어갔어야 할 것을 미리
고를 수 있나" 는 체결된 것만 봐서는 답이 안 나오기 때문이다.

결과변수: 사후수익 ret (그 방향 ETF 를 결정시각 종가에 사서 다음 반대확정/15:00 에 판 %).
feature: f1_feat.py 와 동일(결정시각까지의 정보만, production 함수 호출).
검정: train = 5~7월, test = 8~10월, 추가로 최근 10일 — 구간을 넘어 살아남는지만 본다.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f1_feat as F1  # noqa: E402
import rep2  # noqa: E402

from app.trading.macd2 import config  # noqa: E402

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
FEATS = {"ema_stack": +1, "px_ema10": +1, "px_ema20": +1, "ema_sep": +1,
         "ema10_slope": +1, "ema20_slope": +1, "hist": +1, "hist_slope": +1,
         "t3_gain": +1, "t3_ratio": +1, "vwap_pos": +1, "atr_pct": 0, "vol_rel": 0,
         "pe30": +1, "pe60": +1, "xc60": -1, "rev15": -1, "rev30": -1, "rev60": -1}


def etf(day, tag):
    p = f"{F1.BARS}/replay_{day}_{tag}_1m.csv"
    if not os.path.exists(p):
        return None
    x = pd.read_csv(p)
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    return x.drop_duplicates("datetime", keep="last").set_index("datetime").sort_index()


def build():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    rows = []
    for day in days:
        p = f"{rep2.LED}/BASE_{day}.json"
        if not os.path.exists(p):
            continue
        o = json.load(open(p, encoding="utf-8"))
        fl = []
        for s in o.get("sig", []):
            sid = str(s.get("signal_id") or "")
            if not sid.endswith(":TW2_3SLOT_CONFIRM") or not sid.startswith(day):
                continue
            t = pd.Timestamp(str(s["detected_at"])[:19])
            if not (pd.Timestamp("09:00").time() <= t.time() < pd.Timestamp("15:00").time()):
                continue
            fl.append(dict(t=t, d=1 if s.get("direction") == "UP_RED" else -1, sid=sid,
                           result=s.get("order_result"), why=s.get("block_reason") or ""))
        if not fl:
            continue
        f = pd.DataFrame(fl).drop_duplicates(["t", "d"]).sort_values("t").reset_index(drop=True)
        lg, iv = etf(day, "long"), etf(day, "inverse")
        if lg is None or iv is None:
            continue
        end = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:00")
        raw = F1.raw_flags_of(day)
        for r in f.itertuples():
            e = lg if r.d == 1 else iv
            nxt = f[(f.t > r.t) & (f.d == -r.d)]
            t_out = min(nxt.t.iloc[0], end) if len(nxt) else end
            pi, po = e["close"][e.index <= r.t], e["close"][e.index <= t_out]
            if pi.empty or po.empty:
                continue
            h = str(r.sid).split("_")[1]
            fb = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {h[:2]}:{h[2:4]}:00").tz_localize(config.KST)
            ff = F1.feats(day, r.t.tz_localize(config.KST), r.d, fb, raw)
            rows.append(dict(day=day, t=r.t, hm=r.t.strftime("%H:%M"),
                             hour=r.t.hour, dir=("UP" if r.d == 1 else "DN"),
                             result=r.result, why=r.why,
                             ret=(po.iloc[-1] / pi.iloc[-1] - 1) * 100, **ff))
    return pd.DataFrame(rows)


def auc(y, s):
    m = ~(np.isnan(s) | np.isnan(y))
    y, s = y[m], s[m]
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return np.nan
    r = pd.Series(np.r_[pos, neg]).rank().to_numpy()
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def main():
    fp = OUT + "/h2_flags.csv"
    if os.path.exists(fp) and "--rebuild" not in sys.argv:
        d = pd.read_csv(fp, dtype={"day": str}, parse_dates=["t"])
    else:
        d = build()
        d.to_csv(fp, index=False, encoding="utf-8-sig")
    days = sorted(d.day.unique())
    d["seg"] = np.where(d.day < "20260801", "5~7월", np.where(d.day < "20260901", "8월", "9~10월"))
    r10 = d.day.isin(days[-10:])
    print(f"확정플래그 {len(d)}건 / {d.day.nunique()}일  (양수 {(d.ret > 0).mean() * 100:.0f}%)\n")

    print("=" * 110)
    print("1) 시간대별 — 돈이 어디 있었나")
    print("=" * 110)
    bins = [(9, "09시"), (10, "10시"), (11, "11시"), (12, "12시"), (13, "13시"), (14, "14시")]
    for seg in ("5~7월", "8월", "9~10월"):
        g = d[d.seg == seg]
        print(f"\n  [{seg}]  n {len(g)}")
        print(f"  {'시간':6s} {'n':>4s} {'승률':>6s} {'평균':>7s} {'중앙':>7s} {'합':>8s} "
              f"{'체결':>4s} {'체결평균':>8s} {'상위10%평균':>10s}")
        for h, lab in bins:
            x = g[g.hour == h]
            if not len(x):
                continue
            ex = x[x.result == "EXECUTED"]
            top = x.ret.quantile(0.9)
            print(f"  {lab:6s} {len(x):4d} {(x.ret > 0).mean() * 100:5.1f}% {x.ret.mean():+7.2f} "
                  f"{x.ret.median():+7.2f} {x.ret.sum():+8.1f} {len(ex):4d} "
                  f"{(ex.ret.mean() if len(ex) else np.nan):+8.2f} {x[x.ret >= top].ret.mean():+10.2f}")
    print(f"\n  [최근 10일]  n {int(r10.sum())}")
    g = d[r10]
    for h, lab in bins:
        x = g[g.hour == h]
        if not len(x):
            continue
        print(f"  {lab:6s} {len(x):4d} {(x.ret > 0).mean() * 100:5.1f}% {x.ret.mean():+7.2f} "
              f"{x.ret.median():+7.2f} {x.ret.sum():+8.1f}")

    print("\n" + "=" * 110)
    print("2) 사전에 고를 수 있나 — feature 별 AUC (결과 = 사후수익 > 0)")
    print("=" * 110)
    y = (d.ret > 0).astype(float).to_numpy()
    tr = (d.day < "20260801").to_numpy()
    te = ((d.day >= "20260801")).to_numpy()
    rng = np.random.default_rng(0)
    uniq = d.day.unique()
    alld = d.day.to_numpy()
    print(f"  {'feature':12s} {'전체':>6s} {'95% CI':>16s} {'train 5~7':>10s} {'test 8~10':>10s} {'최근10일':>9s}")
    res = []
    for f, sg in FEATS.items():
        s = (sg if sg else 1) * d[f].astype(float).to_numpy()
        a = auc(y, s)
        bs = []
        for _ in range(500):
            pick = rng.choice(uniq, len(uniq))
            idx = np.concatenate([np.where(alld == k)[0] for k in pick])
            bs.append(auc(y[idx], s[idx]))
        lo, hi = np.nanpercentile(bs, [2.5, 97.5])
        res.append((f, sg, a, lo, hi, auc(y[tr], s[tr]), auc(y[te], s[te]), auc(y[r10.to_numpy()], s[r10.to_numpy()])))
    for f, sg, a, lo, hi, a1, a2, a3 in sorted(res, key=lambda x: -(x[2] if x[2] == x[2] else 0)):
        mark = " <<<" if (lo > 0.5 or hi < 0.5) else ""
        print(f"  {('+' if sg >= 0 else '-') + f:12s} {a:6.3f} [{lo:.3f},{hi:.3f}] {a1:10.3f} "
              f"{a2:10.3f} {a3:9.3f}{mark}")

    print("\n" + "=" * 110)
    print("3) '시간대' 자체가 신호인가 — 11:00 이후 플래그만 골랐다면")
    print("=" * 110)
    for seg, m in (("5~7월", d.seg == "5~7월"), ("8월", d.seg == "8월"),
                   ("9~10월", d.seg == "9~10월"), ("최근10일", r10)):
        g = d[m]
        for lab, mm in (("전체", g.ret == g.ret), ("09~10시", g.hour < 11), ("11시 이후", g.hour >= 11)):
            x = g[mm]
            print(f"  {seg:8s} {lab:9s} n {len(x):4d} 승률 {(x.ret > 0).mean() * 100:5.1f}% "
                  f"평균 {x.ret.mean():+6.2f} 합 {x.ret.sum():+8.1f}")
        print()


if __name__ == "__main__":
    main()
