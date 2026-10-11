"""CHOP 손절 재연구 — -1% 즉시 / 유예 / 기한청산 비교. READ-ONLY.

경로계산은 ETF 실제 1분봉으로 하고, 그 계산이 이미 돌린 20분 유예 full replay(B)의
11건 결과를 재현하는지 **먼저 교정**한 뒤에만 다른 규칙에 적용한다.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
SPD = W + "/sp/data"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
TAG = {"0193T0": "long", "0197X0": "inverse"}
_c = {}


def bars(day, sym):
    k = (day, sym)
    if k not in _c:
        p = f"{SPD}/replay_{day}_{TAG[sym]}_1m.csv"
        if not os.path.exists(p):
            _c[k] = None
        else:
            d = pd.read_csv(p)
            d["datetime"] = pd.to_datetime(d["datetime"])
            _c[k] = d.sort_values("datetime").reset_index(drop=True)
    return _c[k]


def A(d):
    for p in (f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def B(d):
    p = f"{W}/out/REAL_STRUCTSTOP_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else A(d)


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


# ── 1) 대상 : 85일 B3_SL 11건 ───────────────────────────────────────────────
rows = []
for d in DAYS:
    j = A(d)
    for t in trades(j):
        if "B3_SL" not in "+".join(t["reasons"]):
            continue
        cost = t["net"] - (float(t["legs"][-1][2]) / t["px_in"] - 1) * 100   # net% - gross%
        rows.append(dict(day=d, sym=t["sym"], entry=pd.Timestamp(t["entry"]).tz_localize(None),
                         stop=pd.Timestamp(t["exit"]).tz_localize(None), px_in=t["px_in"],
                         q=t["q"], a_net=t["net"], a_krw=t["krw"], cost=cost))
S = pd.DataFrame(rows)
print("B3_SL %d건 / A 합계 %s" % (len(S), "{:+,.0f}".format(S.a_krw.sum())))

TW_STOP = -1.40      # 유예 중 살아있는 다음 방어선 (TWF 오전 손절). 재생 관측치와 맞춘다.
MAXHOLD = 20.0       # P3_B3_MAX_HOLD_MIN


def simulate(r, wait_min, recover_at=None, hard=TW_STOP, from_entry_cap=MAXHOLD):
    """손절 시점부터 유예. 1분봉 종가로 판정(= production 틱판정의 보수적 근사).
    반환: (net%, 청산사유, 경과분)"""
    b = bars(r.day, r.sym)
    if b is None:
        return r.a_net, "NO_BARS", 0.0
    t0 = r.stop
    end_wait = t0 + pd.Timedelta(minutes=wait_min)
    end_hold = r.entry + pd.Timedelta(minutes=from_entry_cap)
    end = min(end_wait, end_hold)
    w = b[(b.datetime > t0) & (b.datetime <= end)]
    w = w[w.datetime.dt.strftime("%H%M") <= "1520"]
    if not len(w):
        return r.a_net, "IMMEDIATE", 0.0
    for _, x in w.iterrows():
        lo = (float(x.low) / r.px_in - 1) * 100 + r.cost
        hi = (float(x.high) / r.px_in - 1) * 100 + r.cost
        if hard is not None and lo <= hard:                 # 다음 방어선
            return hard, "TW_STOP", (x.datetime - t0).total_seconds() / 60
        if recover_at is not None and hi >= recover_at:
            return recover_at, "RECOVER", (x.datetime - t0).total_seconds() / 60
    last = w.iloc[-1]
    return (float(last.close) / r.px_in - 1) * 100 + r.cost, "TIMEOUT", \
           (last.datetime - t0).total_seconds() / 60


def total(net_list):
    return float(sum(n / 100 * S.q.iloc[i] * S.px_in.iloc[i] for i, n in enumerate(net_list)))


# ── 2) 교정 : 20분 유예 + 회복 -0.2% == full replay B ───────────────────────
sec("0. 교정 — 경로계산이 full replay B(20분 유예, 회복 -0.2%)를 재현하는가")
TB = {}
for d in DAYS:
    for t in trades(B(d)):
        TB[(d, t["sym"], pd.Timestamp(t["entry"]).tz_localize(None))] = t
sim, act = [], []
for i, r in S.iterrows():
    n, why, el = simulate(r, 20.0, recover_at=-0.2)
    tb = TB.get((r.day, r.sym, r.entry))
    sim.append(n)
    act.append(tb["net"] if tb else np.nan)
cal = pd.DataFrame(dict(day=S.day, sym=S.sym, A=S.a_net, 경로계산=sim, 실제replay=act))
cal["차이"] = cal.경로계산 - cal.실제replay
print(cal.to_string(index=False, float_format="{:,.2f}".format))
print("\n  경로계산 합계 %s / 실제 replay 합계 %s / 평균 절대차 %.2f%%p"
      % ("{:+,.0f}".format(total(sim)),
         "{:+,.0f}".format(float(sum(cal.실제replay / 100 * S.q * S.px_in))),
         cal["차이"].abs().mean()))

# ── 3) 규칙 비교 ───────────────────────────────────────────────────────────
sec("1. 규칙별 11건 합계 (경로계산, 다음 방어선 -1.40% 포함)")
res = []
res.append(dict(규칙="R0 즉시 -1.0% 손절 (현행)", 합계=float(S.a_krw.sum()),
                평균net=S.a_net.mean(), 최악net=S.a_net.min(), 깊은손실=int((S.a_net < -1.0).sum())))
for wmin in (5, 10, 15, 20, 30):
    nets = [simulate(r, wmin)[0] for _, r in S.iterrows()]
    res.append(dict(규칙=f"R1 유예 {wmin:>2d}분 후 종가청산", 합계=total(nets),
                    평균net=np.mean(nets), 최악net=min(nets), 깊은손실=int(sum(1 for n in nets if n < -1.0))))
for rec in (-0.5, -0.2, 0.0):
    nets = [simulate(r, 20.0, recover_at=rec)[0] for _, r in S.iterrows()]
    res.append(dict(규칙=f"R2 20분 유예 + {rec:+.1f}% 회복청산", 합계=total(nets),
                    평균net=np.mean(nets), 최악net=min(nets), 깊은손실=int(sum(1 for n in nets if n < -1.0))))
for h in (-1.2, -1.4, -1.7):
    nets = [simulate(r, 20.0, hard=h)[0] for _, r in S.iterrows()]
    res.append(dict(규칙=f"R3 20분 유예 + 하드손절 {h:+.1f}%", 합계=total(nets),
                    평균net=np.mean(nets), 최악net=min(nets), 깊은손실=int(sum(1 for n in nets if n < -1.0))))
o = pd.DataFrame(res)
o["Δ"] = o.합계 - o.합계.iloc[0]
print(o.to_string(index=False, formatters={"합계": "{:+,.0f}".format, "Δ": "{:+,.0f}".format,
                                           "평균net": "{:.2f}".format, "최악net": "{:.2f}".format}))

# ── 4) 더 빠른 손절 ────────────────────────────────────────────────────────
sec("2. 반대 방향 — 더 빨리 자르면? (전체 CHOP 포지션 경로 기준)")
cr = []
for d in DAYS:
    j = A(d)
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in (j.get("entries") or [])}
    for t in trades(j):
        e = ent.get(pd.Timestamp(t["entry"]).strftime("%H:%M:%S"), {})
        if e.get("regime") != "CHOP":
            continue
        b = bars(d, t["sym"])
        if b is None:
            continue
        cost = t["net"] - (float(t["legs"][-1][2]) / t["px_in"] - 1) * 100
        e0 = pd.Timestamp(t["entry"]).tz_localize(None)
        e1 = pd.Timestamp(t["exit"]).tz_localize(None)
        w = b[(b.datetime >= e0) & (b.datetime <= e1)]
        if not len(w):
            continue
        mae = (w.low.min() / t["px_in"] - 1) * 100 + cost
        cr.append(dict(day=d, sym=t["sym"], net=t["net"], krw=t["krw"], mae=mae,
                       q=t["q"], px=t["px_in"], cost=cost, e0=e0, e1=e1))
C = pd.DataFrame(cr)
print("CHOP 포지션 %d건 / 합계 %s" % (len(C), "{:+,.0f}".format(C.krw.sum())))
out = []
for lvl in (-0.4, -0.5, -0.6, -0.7, -0.8, -1.0):
    hit = C[C.mae <= lvl]
    # 그 레벨에서 잘랐을 때: 도달한 건은 lvl 로 확정, 나머지는 실제 결과 유지
    k = float((C[C.mae > lvl].krw).sum() + sum(lvl / 100 * hit.q * hit.px))
    out.append(dict(손절선=f"{lvl:.1f}%", 도달건수=len(hit),
                    도달건_실제합=float(hit.krw.sum()),
                    잘랐을때=float(sum(lvl / 100 * hit.q * hit.px)),
                    전체합계=k))
x = pd.DataFrame(out)
x["Δ"] = x.전체합계 - float(C.krw.sum())
print(x.to_string(index=False, formatters={c: "{:+,.0f}".format for c in
                                           ["도달건_실제합", "잘랐을때", "전체합계", "Δ"]}))

sec("3. CHOP 포지션의 MAE 분포 — '얼마나 빠지면 못 돌아오나'")
C["결과"] = np.where(C.krw > 0, "수익", "손실")
bins = [-99, -1.0, -0.8, -0.6, -0.4, -0.2, 0.0, 99]
lab = ["≤-1.0", "-1.0~-0.8", "-0.8~-0.6", "-0.6~-0.4", "-0.4~-0.2", "-0.2~0", ">0"]
C["mae_bin"] = pd.cut(C.mae, bins=bins, labels=lab)
g = C.groupby("mae_bin", observed=False).agg(건수=("krw", "size"), 수익건=("krw", lambda s: int((s > 0).sum())),
                                             합계=("krw", "sum"), 평균net=("net", "mean"))
g["수익률%"] = (g.수익건 / g.건수.replace(0, np.nan) * 100)
print(g.to_string(float_format="{:,.1f}".format))
