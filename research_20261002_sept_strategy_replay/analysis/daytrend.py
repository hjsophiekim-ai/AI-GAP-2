"""DAY-TREND HOLD 전략 (재설계안) — READ-ONLY 연구. production 무관, 1분봉 캐시로 독립 시뮬.

아이디어(사용자): 그날 전체 추세를 보고, 오전에 추세 방향 플래그 하나를 잡아 오래 들고 간다.
사전 고정 규칙 (그리드 탐색 없음):
  추세 판정(판정 시점까지의 정보만): 하이닉스 종가가 (a) 09:00 시가 위 (b) 09:00~ VWAP 위
     (c) 3분 EMA20 > EMA50 -> UP. 셋 다 반대 -> DOWN. 그 외 NONE(진입 안 함).
  진입: 09:15~11:30 사이 3분 MACD 히스토그램 0선 교차 플래그 중 추세 방향 것,
     플래그 봉 뒤 2봉 동안 부호 유지(T+3 근사) -> 확인봉 완성 시점의 ETF 1분 종가 + 1틱(5원).
     하루 1회 (DT-A) / 손절 시 같은 방향 1회 재진입 (DT-B).
  청산:
     E1 15:00 까지 보유, 안전손절 ETF -3%
     E2 추세붕괴: 3분 종가가 VWAP 반대편 + 반대 플래그 확인 둘 다, 안전손절 -3%, 15:00
     E3 안전손절 -3%, +3% 이후 고점대비 -2% 반납 시 청산, 15:00
  비용: 왕복 0.03% + 매도 1틱.
비교: R0(현행 P3) 하루 net% 합 (엔진 83일 + 0930/1001 은 production replay).
"""
import glob, json, os, pickle, sys
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
C = r"C:/Users/FURSYS/Desktop/AI-GAP 2/research_20260930_p3_studies/engine/cache84"
TICK, FEE, SL, ARM, GIVE = 5.0, 0.03, 3.0, 3.0, 2.0


def rd(path):
    x = pd.read_csv(path, parse_dates=["datetime"])
    if x["datetime"].dt.tz is not None:
        x["datetime"] = x["datetime"].dt.tz_localize(None)
    return x


def load(d, tag):
    p = f"{S}/wk/data/replay_{d}_{tag}_1m.csv"
    if d in ("20260930", "20261001") and os.path.exists(p):
        return rd(p)
    return rd(f"{C}/replay_{d}_{tag}_1m.csv")


days = sorted({os.path.basename(p)[7:15] for p in glob.glob(C + "/replay_*_hynix_1m.csv")} | {"20261001"})
days = [d for d in days if d >= "20260527"]
hy_all = pd.concat([load(d, "hynix") for d in days]).drop_duplicates("datetime").sort_values("datetime")
hm = hy_all["datetime"].dt.hour * 60 + hy_all["datetime"].dt.minute
hy_all = hy_all[hm.between(8 * 60, 15 * 60 + 30) & ~hm.between(8 * 60 + 50, 8 * 60 + 59)].reset_index(drop=True)
b = hy_all.set_index("datetime").groupby(pd.Grouper(freq="3min", label="left", closed="left")).agg(
    open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
    volume=("volume", "sum")).dropna().reset_index()
c = b["close"]
macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
b["hist"] = macd - macd.ewm(span=9, adjust=False).mean()
b["ema20"] = c.ewm(span=20, adjust=False).mean()
b["ema50"] = c.ewm(span=50, adjust=False).mean()
b["done"] = b["datetime"] + pd.Timedelta(minutes=3)
b["day"] = b["datetime"].dt.strftime("%Y%m%d")
sgn = np.sign(b["hist"].to_numpy())
flag = np.zeros(len(b), dtype=int)
flag[1:] = np.where((sgn[1:] != sgn[:-1]) & (sgn[1:] != 0), sgn[1:], 0)
b["flag"] = flag
ETF = {}


def etf(d, f):
    k = (d, f)
    if k not in ETF:
        e = load(d, "long" if f == 1 else "inverse")
        ETF[k] = e[e["datetime"].dt.strftime("%Y%m%d") == d].reset_index(drop=True)
    return ETF[k]


def day_frame(d):
    x = b[b["day"] == d].copy()
    reg = x[x["datetime"].dt.hour >= 9]
    if len(reg) == 0:
        return None
    op = float(reg["open"].iloc[0])
    tp = (reg["high"] + reg["low"] + reg["close"]) / 3
    vw = (tp * reg["volume"]).cumsum() / reg["volume"].cumsum().replace(0, np.nan)
    x["vwap"] = vw.reindex(x.index)
    x["op"] = op
    return x


def trend_at(row):
    up = [row["close"] > row["op"], row["close"] > row["vwap"], row["ema20"] > row["ema50"]]
    if all(up):
        return 1
    if not any(up):
        return -1
    return 0


def confirmed_flags(x):
    out = []
    idx = list(x.index)
    for k, i in enumerate(idx):
        f = x.at[i, "flag"]
        if f == 0 or k + 2 >= len(idx):
            continue
        j1, j2 = idx[k + 1], idx[k + 2]
        if np.sign(x.at[j1, "hist"]) == f and np.sign(x.at[j2, "hist"]) == f:
            out.append((x.at[j2, "done"], int(f), x.at[i, "datetime"], j2))
    return out


FL = {}


def run_day(d, exit_rule, reentry):
    if d not in FL:
        x = day_frame(d)
        FL[d] = (x, confirmed_flags(x) if x is not None else [])
    x, fl = FL[d]
    if x is None:
        return [], None
    trades, used, t_free, stopped_dir = [], 0, None, None
    end = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00")
    first = None
    for ct, f, fbt, j in fl:
        hhmm = ct.hour * 60 + ct.minute
        if not (9 * 60 + 15 <= hhmm <= 11 * 60 + 30):
            continue
        if t_free is not None and ct < t_free:
            continue
        if used >= 1 and (not reentry or used >= 2 or stopped_dir != f):
            break
        tr = trend_at(x.loc[j])
        if first is None:
            first = (ct.strftime("%H:%M"), "UP" if f == 1 else "DN", {1: "UP", -1: "DN", 0: "NONE"}[tr])
        if tr != f:
            continue
        e = etf(d, f)
        seen = e[e["datetime"] + pd.Timedelta(minutes=1) <= ct]
        if len(seen) == 0:
            continue
        px_in = float(seen["close"].iloc[-1]) + TICK
        path = e[e["datetime"] + pd.Timedelta(minutes=1) > ct]
        opp = [o[0] for o in fl if o[0] > ct and o[1] != f]
        peak, xt, xp, why = 0.0, None, None, None
        for r in path.itertuples():
            t = r.datetime + pd.Timedelta(minutes=1)
            ret = (r.close / px_in - 1) * 100
            peak = max(peak, (r.high / px_in - 1) * 100)
            if ret <= -SL:
                xt, xp, why = t, r.close, "SL"; break
            if exit_rule == "E3" and peak >= ARM and ret <= peak - GIVE:
                xt, xp, why = t, r.close, "TRAIL"; break
            if exit_rule == "E2" and any(o <= t for o in opp):
                bb = x[x["done"] <= t]
                if len(bb) and (bb["close"].iloc[-1] - bb["vwap"].iloc[-1]) * f < 0:
                    xt, xp, why = t, r.close, "TREND_BREAK"; break
            if t >= end:
                xt, xp, why = t, r.close, "15:00"; break
        if xt is None:
            r = path.iloc[-1]
            xt, xp, why = r["datetime"] + pd.Timedelta(minutes=1), r["close"], "EOD"
        net = ((xp - TICK) / px_in - 1) * 100 - FEE
        trades.append(dict(day=d, dir="UP" if f == 1 else "DN", flag=fbt.strftime("%H:%M"), entry=ct.strftime("%H:%M"),
                           px_in=px_in, exit=xt.strftime("%H:%M"), px_out=xp - TICK, why=why, net=net, peak=peak))
        used += 1
        t_free = xt
        stopped_dir = f if why == "SL" else None
    return trades, first


r0 = {}
for t in pickle.load(open(S + "/research_20260930_highvol_day_mode/lab/out_H30_d83.pkl", "rb"))["trades"]:
    r0[t["date"]] = r0.get(t["date"], 0.0) + t["net_pct"]


def replay_day_net(d):
    p = f"{S}/wk/out/wk_{d}.json"
    if not os.path.exists(p):
        return None
    o = json.load(open(p, encoding="utf-8"))
    s, cur = 0.0, None
    for r in o["orders"]:
        if r["side"] == "BUY":
            cur = dict(px=r["px"], q=r["qty"], sold=0, pr=0.0)
        elif cur:
            cur["sold"] += r["qty"]; cur["pr"] += r["qty"] * r["px"]
        if cur and cur["sold"] >= cur["q"]:
            s += (cur["pr"] / cur["q"] / cur["px"] - 1) * 100 - FEE
            cur = None
    return s


for d in ("20260930", "20261001"):
    v = replay_day_net(d)
    if v is not None:
        r0[d] = v

WEEK = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
VARS = (("DT-A/E1", "E1", False), ("DT-A/E2", "E2", False), ("DT-A/E3", "E3", False),
        ("DT-B/E1", "E1", True), ("DT-B/E2", "E2", True), ("DT-B/E3", "E3", True))
res = {}
for name, er, re_ in VARS:
    allt = []
    for d in days:
        allt += run_day(d, er, re_)[0]
    res[name] = pd.DataFrame(allt)
pickle.dump(res, open(S + "/wk/lab/daytrend_res.pkl", "wb"))


def stats(daily):
    v = np.array(daily, dtype=float)
    eq = np.cumprod(1 + v / 100)
    dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
    return f"합 {v.sum():+7.2f}  복리 {(eq[-1] - 1) * 100:+8.2f}  MDD {dd:6.2f}  수익일 {(v > 0).sum():2d}/{len(v)} 손실일 {(v < 0).sum():2d}"


print("R0 0930/1001 replay:", {d: round(r0.get(d, float('nan')), 2) for d in ("20260930", "20261001")})
print("=== 하루 net% (사이즈 1.0 가정; R0 는 엔진 실현 net 합) — 10/01 은 14:07 까지")
for scope, ds in (("전체 0527~1001", days), ("9월~", [d for d in days if d >= "20260901"]),
                  ("주간 0922~1001", WEEK), ("0527~0831 (앞)", [d for d in days if d < "20260901"])):
    print(f"\n[{scope}] {len(ds)}일")
    print("  R0 현행 P3     " + stats([r0.get(d, 0.0) for d in ds]))
    for name, df in res.items():
        dd = df.groupby("day")["net"].sum() if len(df) else pd.Series(dtype=float)
        n = int(df["day"].isin(ds).sum()) if len(df) else 0
        print(f"  {name:14s} " + stats([float(dd.get(d, 0.0)) for d in ds]) + f"  거래 {n}")
print("\n=== 주간 거래 상세")
for name in ("DT-A/E1", "DT-A/E2", "DT-A/E3", "DT-B/E2"):
    df = res[name]
    w = df[df["day"].isin(WEEK)]
    print(f"\n[{name}]")
    for d in WEEK:
        tr = w[w["day"] == d]
        _, first = run_day(d, "E1", False)
        line = " | ".join(f"{r.dir} 플래그{r.flag} 진입{r.entry}@{r.px_in:.0f} -> {r.exit}@{r.px_out:.0f} {r.why} {r.net:+.2f}% (MFE {r.peak:+.2f})"
                          for r in tr.itertuples())
        print(f"  {d}  R0 {r0.get(d, 0):+.2f}%  | DT {tr['net'].sum():+.2f}%  첫확인플래그 {first}  {line or '진입 없음'}")
