"""TREND-ONLY-2SLOT vs 기존 E / E-2SLOT — 9월+10월 REAL 재생 분석. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, W)
from feat import trades, _day, ema  # noqa
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 60)

PK = ROOT + "/research_20261008_peaklock/out"
O = W + "/out"
DAYS = [x.split()[1] for x in open(W + "/jobs_TREND2.txt", encoding="utf-8")]


def apath(v, d):
    if v == "A":
        for p in (f"{O}/REAL_A1008_{d}.json", f"{O}/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
            if os.path.exists(p):
                return p
        return None
    p = f"{O}/REAL_{v}_{d}.json"
    return p if os.path.exists(p) else None


VARS = ["A", "E2SLOT", "TREND2", "TREND3"]
NM = {"A": "A 기존 E (3슬롯)", "E2SLOT": "B E-2SLOT", "TREND2": "C TREND-ONLY-2SLOT (연속2봉)",
      "TREND3": "D TREND-ONLY-2SLOT (연속3봉)"}
J, T = {}, {}
for v in VARS:
    J[v] = {}
    for d in DAYS:
        p = apath(v, d)
        if p:
            J[v][d] = json.load(open(p, encoding="utf-8"))
    T[v] = sum((trades(J[v][d]) for d in sorted(J[v])), [])
    print("%-8s 재생 %d/%d일  거래 %d" % (v, len(J[v]), len(DAYS), len(T[v])))
HAVE = [v for v in VARS if len(J[v]) == len(DAYS)]
print("완주 변형:", HAVE)


def metrics(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    r = daily / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), 복리=(eq[-1] - 1) * 100,
                PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                승률=(k > 0).mean() * 100, 거래=len(tr), 손실일=int((daily < 0).sum()),
                최대1일손실=daily.min(), 최대1일수익=daily.max(),
                평균보유=np.mean([t["hold_min"] for t in tr]) if tr else 0.0)


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


sec("1. 전체 9월+10월 (%d영업일)" % len(DAYS))
rows = [dict(변형=NM[v], **metrics(DAYS, T[v])) for v in HAVE]
o = pd.DataFrame(rows)
o["Δ총손익"] = o.총손익 - o.총손익.iloc[0]
print(o.to_string(index=False, formatters={
    "총손익": "{:+,.0f}".format, "Δ총손익": "{:+,.0f}".format,
    "최대1일손실": "{:+,.0f}".format, "최대1일수익": "{:+,.0f}".format,
    "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format,
    "복리": "{:+.1f}".format, "평균보유": "{:.0f}".format}))

sec("2. 9월(18일) / 10월(5일) 분리")
for lab, dd in (("9월", [d for d in DAYS if d < "20261001"]), ("10월", [d for d in DAYS if d >= "20261001"])):
    r = [dict(변형=NM[v], **metrics(dd, [t for t in T[v] if t["day"] in dd])) for v in HAVE]
    x = pd.DataFrame(r)
    x["Δ"] = x.총손익 - x.총손익.iloc[0]
    print("\n[%s]" % lab)
    print(x[["변형", "총손익", "PF", "MDD", "손실일", "거래", "승률", "최대1일손실", "Δ"]].to_string(
        index=False, formatters={"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format,
                                 "최대1일손실": "{:+,.0f}".format, "PF": "{:.2f}".format,
                                 "MDD": "{:.2f}".format, "승률": "{:.1f}".format}))

sec("3. candidate 동작 / false trend / 진입지연")
for v in HAVE:
    ev = [e for d in sorted(J[v]) for e in (J[v][d].get("brk") or [])]
    c = pd.Series([e["ev"] for e in ev]).value_counts().to_dict()
    fired = [e for e in ev if e["ev"] == "FIRED" and e.get("executed")]
    dly = [float(e["delay_min"]) for e in fired if e.get("delay_min") is not None]
    tt = T[v]
    lose = [t for t in tt if t["krw"] < 0]
    print("\n[%s]" % NM[v])
    print("  이벤트:", c)
    print("  진입 %d건 / 손실진입(false trend) %d건 (%.0f%%) %s원 / 평균 진입지연 %.2f분 (중앙 %.2f)"
          % (len(tt), len(lose), 100 * len(lose) / max(len(tt), 1),
             "{:+,.0f}".format(sum(t["krw"] for t in lose)),
             float(np.mean(dly)) if dly else 0.0, float(np.median(dly)) if dly else 0.0))

sec("4. 놓친 큰 추세 — A 가 번 큰 거래를 후보가 안 잡았나")
Akey = {(t["day"], t["sid"].replace(":BRK", "").split(":TW2")[0]): t for t in T["A"]}
for v in [x for x in HAVE if x != "A"]:
    vk = {(t["day"], t["sid"].replace(":BRK", "").split(":TW2")[0]) for t in T[v]}
    miss = [t for k, t in Akey.items() if k not in vk]
    big = [t for t in miss if t["net"] >= 2.0]
    print("\n[%s] A 에만 있던 진입 %d건 %s원 / 그중 net>=+2%% 큰 추세 %d건 %s원"
          % (NM[v], len(miss), "{:+,.0f}".format(sum(t['krw'] for t in miss)),
             len(big), "{:+,.0f}".format(sum(t['krw'] for t in big))))
    for t in sorted(big, key=lambda x: -x["krw"])[:8]:
        print("   %s %s net %+.2f%% %s원" % (t["day"], str(t["entry"])[11:16], t["net"],
                                             "{:+,.0f}".format(t["krw"])))
    newt = [t for t in T[v] if (t["day"], t["sid"].replace(":BRK", "").split(":TW2")[0]) not in Akey]
    print("   후보에만 있던 진입 %d건 %s원" % (len(newt), "{:+,.0f}".format(sum(t['krw'] for t in newt))))

sec("5. 10/01~10/08 일별")
oct_ = [d for d in DAYS if d >= "20261001"]
rows = []
for d in oct_:
    r = {"day": d}
    for v in HAVE:
        r[NM[v]] = sum(t["krw"] for t in T[v] if t["day"] == d)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False, float_format="{:+,.0f}".format))

sec("6. 2026-10-08 상세")
for v in HAVE:
    if "20261008" not in J[v]:
        continue
    j = J[v]["20261008"]
    print("\n[%s]" % NM[v])
    for t in trades(j):
        print("   진입 %s %s qty %d @ %.0f -> %s  net %+.2f%% %s원" %
              (str(t["entry"])[11:16], t["sym"], t["q"], t["px_in"], "+".join(dict.fromkeys(t["reasons"])),
               t["net"], "{:+,.0f}".format(t["krw"])))
    for e in (j.get("brk") or []):
        if e["ev"] in ("ARMED_BRK15", "TREND_OK", "FIRED", "EXPIRED", "DAYCAP_BLOCK", "DAYCAP_DROP", "SUPERSEDED"):
            print("   %-13s %s %s" % (e["ev"], e.get("at", e.get("t", ""))[11:19],
                                      {k: e[k] for k in ("sid", "a", "b", "c", "d", "executed", "delay_min") if k in e}))
