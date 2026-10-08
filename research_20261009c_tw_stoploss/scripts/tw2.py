"""TW 손절을 줄이는 유일한 남은 지렛대 — 반납(giveback) 방어 정확계산. READ-ONLY.

152건 전부의 1분봉 경로를 순서대로 걸어 'peak>=X 이후 net<=F 로 되밀리면 청산' 규칙을
**정확히** 평가한다(경로 순서가 있으므로 추정이 아니다). 단 진입집합/슬롯 연쇄효과는
재생이 필요하므로 1차 산술이다.

기존 PEAK-LOCK(2026-10-08)은 peak>=2.0 에서 85일 -2,625,090 으로 기각됐다. 여기서는
TW 손절군의 실제 peak 대역(평균 1.77%)을 포함해 더 낮은 구간까지 본다.
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
pd.set_option("display.width", 250)

DAYS = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
TAG = {"0193T0": "long", "0197X0": "inverse"}
_c = {}


def bars(day, sym):
    k = (day, sym)
    if k not in _c:
        p = f"{SPD}/replay_{day}_{TAG[sym]}_1m.csv"
        if os.path.exists(p):
            d = pd.read_csv(p)
            d["datetime"] = pd.to_datetime(d["datetime"])
            if getattr(d["datetime"].dt, "tz", None) is not None:
                d["datetime"] = d["datetime"].dt.tz_localize(None)
            _c[k] = d.sort_values("datetime").reset_index(drop=True)
        else:
            _c[k] = None
    return _c[k]


def A(d):
    for p in (f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


P = []
for d in DAYS:
    j = A(d)
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in (j.get("entries") or [])}
    for t in trades(j):
        b = bars(d, t["sym"])
        if b is None:
            continue
        e0 = pd.Timestamp(t["entry"]).tz_localize(None)
        e1 = pd.Timestamp(t["exit"]).tz_localize(None)
        w = b[(b.datetime >= e0) & (b.datetime <= e1)]
        if not len(w):
            continue
        cost = t["net"] - (float(t["legs"][-1][2]) / t["px_in"] - 1) * 100
        e = ent.get(e0.strftime("%H:%M:%S"), {})
        P.append(dict(day=d, sym=t["sym"], q=t["q"], px=t["px_in"], net=t["net"], krw=t["krw"],
                      reason="+".join(dict.fromkeys(t["reasons"])), regime=e.get("regime"),
                      hi=(w.high.to_numpy(float) / t["px_in"] - 1) * 100 + cost,
                      lo=(w.low.to_numpy(float) / t["px_in"] - 1) * 100 + cost,
                      cl=(w.close.to_numpy(float) / t["px_in"] - 1) * 100 + cost))
print("경로 확보 %d건 / 합계 %s" % (len(P), "{:+,.0f}".format(sum(p["krw"] for p in P))))


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


def run_rule(arm, floor):
    """peak(장중 고가 기준) >= arm 을 찍은 뒤 종가가 floor 이하로 되밀리면 그 봉에서 청산.
    발동하지 않으면 실제 결과 유지. 보수적으로 종가 기준(틱보다 늦게 반응)."""
    tot, fired, saved, hurt = 0.0, 0, 0, 0
    for p in P:
        peak = -99.0
        out = None
        for i in range(len(p["hi"])):
            peak = max(peak, p["hi"][i])
            if peak >= arm and p["cl"][i] <= floor:
                out = p["cl"][i]
                break
        if out is None:
            tot += p["krw"]
        else:
            k = out / 100 * p["q"] * p["px"]
            tot += k
            fired += 1
            if k > p["krw"]:
                saved += 1
            elif k < p["krw"]:
                hurt += 1
    return tot, fired, saved, hurt


base = sum(p["krw"] for p in P)
sec("1. peak 방어 격자 — arm(발동 peak) x floor(되밀림 청산선)")
rows = []
for arm in (0.8, 1.0, 1.2, 1.5, 2.0):
    for floor in (0.0, 0.2, 0.3, 0.5):
        if floor >= arm:
            continue
        tot, f, s, h = run_rule(arm, floor)
        rows.append(dict(arm=arm, floor=floor, 발동=f, 개선=s, 악화=h,
                         총손익=tot, Δ=tot - base))
o = pd.DataFrame(rows)
print(o.to_string(index=False, formatters={"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format,
                                           "arm": "{:.1f}".format, "floor": "{:.1f}".format}))

sec("2. 왜 막히는가 — MFE 1%+ 를 찍은 거래는 누구인가")
mfe = np.array([p["hi"].max() for p in P])
krw = np.array([p["krw"] for p in P])
net = np.array([p["net"] for p in P])
for lvl in (0.8, 1.0, 1.5, 2.0, 3.0):
    m = mfe >= lvl
    print("  MFE >= %.1f%% : %3d건 / 합계 %s / 수익건 %d (%.0f%%) / 그중 net>=+2%% %d건 %s"
          % (lvl, m.sum(), "{:+,.0f}".format(krw[m].sum()), int((krw[m] > 0).sum()),
             100 * (krw[m] > 0).mean(), int(((net >= 2.0) & m).sum()),
             "{:+,.0f}".format(krw[(net >= 2.0) & m].sum())))

sec("3. TW 손절군만 떼어보면 (상한)")
tw = [p for p in P if "TIME_WINDOW_STOP_LOSS" in p["reason"]]
print("  TW 손절 %d건 합계 %s" % (len(tw), "{:+,.0f}".format(sum(p["krw"] for p in tw))))
for arm, floor in ((0.8, 0.2), (1.0, 0.3), (1.5, 0.5)):
    tot = 0.0
    fired = 0
    for p in tw:
        peak, out = -99.0, None
        for i in range(len(p["hi"])):
            peak = max(peak, p["hi"][i])
            if peak >= arm and p["cl"][i] <= floor:
                out = p["cl"][i]
                break
        if out is None:
            tot += p["krw"]
        else:
            tot += out / 100 * p["q"] * p["px"]
            fired += 1
    print("  arm %.1f / floor %.1f : 발동 %d건 -> %s (Δ %s)  ※ 승자 훼손 미포함 상한"
          % (arm, floor, fired, "{:+,.0f}".format(tot), "{:+,.0f}".format(tot - sum(p["krw"] for p in tw))))

sec("4. 시간 기반 조기이탈 — 진입 후 N분 시점 net <= T 면 청산")
rows = []
for N in (5, 10, 15, 20):
    for T in (0.0, -0.3, -0.5):
        tot, fired, saved, hurt = 0.0, 0, 0, 0
        for p in P:
            i = min(N, len(p["cl"]) - 1)
            if p["cl"][i] <= T:
                k = p["cl"][i] / 100 * p["q"] * p["px"]
                tot += k; fired += 1
                saved += int(k > p["krw"]); hurt += int(k < p["krw"])
            else:
                tot += p["krw"]
        rows.append(dict(N분=N, 기준=T, 발동=fired, 개선=saved, 악화=hurt, 총손익=tot, Δ=tot - base))
print(pd.DataFrame(rows).to_string(index=False, formatters={
    "총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "기준": "{:+.1f}".format}))

sec("5. TW 손절 33건이 N분 시점에 어떤 상태였나")
rows = []
for N in (5, 10, 15, 20):
    v = [p["cl"][min(N, len(p["cl"]) - 1)] for p in tw]
    a = [p["cl"][min(N, len(p["cl"]) - 1)] for p in P if p["krw"] > 0]
    rows.append(dict(N분=N, TW손절_중앙net=float(np.median(v)), TW손절_음수비율=100 * np.mean(np.array(v) <= 0),
                     수익거래_중앙net=float(np.median(a)), 수익거래_음수비율=100 * np.mean(np.array(a) <= 0)))
print(pd.DataFrame(rows).to_string(index=False, float_format="{:.2f}".format))
