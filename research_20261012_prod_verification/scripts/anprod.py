"""production P3 / P3+SLOW-TP2 / CHOP — 9~10월 23영업일 실측. READ-ONLY.

연구값(전략 E 위에서 측정)과 **구분**해서 본다. production 기준선은 P3 다.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
A3 = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261002_sept_strategy_replay/round3_85d/replay_json"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]


def P3(d):
    """production P3 기준선 (SLOW-TP2 OFF)."""
    for p in (f"{W}/out/REAL_PBASE_{d}.json", f"{W}/out/REAL_APROD_{d}.json",
              f"{A3}/REAL_A_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def V(tag, d, fallback=P3):
    p = f"{W}/out/REAL_{tag}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else fallback(d)


def SLOW(d):
    return V("PSLOW", d)


def CHOP(d):
    return V("PCHOP", d)


def met(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    r = daily / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), 복리=(eq[-1] - 1) * 100,
                PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                최대1일손실=daily.min(), 손실일=int((daily < 0).sum()),
                승률=(k > 0).mean() * 100, 거래=len(tr))


FM = {"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "최대1일손실": "{:+,.0f}".format,
      "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format, "복리": "{:+.1f}".format}


def sec(t):
    print("\n" + "=" * 110 + "\n" + t + "\n" + "=" * 110)


sec("0. 무결성 / 재생 보유현황")
miss = [d for d in DAYS if P3(d) is None]
print("P3 기준선 미확보:", miss or "없음")
nchop = sum(1 for d in DAYS if os.path.exists(f"{W}/out/REAL_PCHOP_{d}.json"))
nslow = sum(1 for d in DAYS if os.path.exists(f"{W}/out/REAL_PSLOW_{d}.json"))
print(f"CHOP 재생 {nchop}일 / SLOW 재생 {nslow}일 / 나머지는 미발동 -> P3 와 동일")

T3 = sum((trades(P3(d)) for d in DAYS), [])
TS = sum((trades(SLOW(d)) for d in DAYS), [])
TC = sum((trades(CHOP(d)) for d in DAYS), [])

sec("1. production 실측 — 9~10월 23영업일 (2026-09-03 ~ 10-08)")
rows = [dict(변형="P3 (현행 production)", **met(DAYS, T3)),
        dict(변형="P3 + SLOW-TP2", **met(DAYS, TS)),
        dict(변형="CHOP (P3+SLOW+2SLOT+MC+구조손절)", **met(DAYS, TC))]
o = pd.DataFrame(rows)
o["Δ"] = o.총손익 - o.총손익.iloc[0]
print(o.to_string(index=False, formatters=FM))

sec("2. 일별 (차이 나는 날만)")
rows = []
for d in DAYS:
    a = sum(t["krw"] for t in T3 if t["day"] == d)
    s = sum(t["krw"] for t in TS if t["day"] == d)
    c = sum(t["krw"] for t in TC if t["day"] == d)
    if a != s or a != c:
        rows.append(dict(day=d, P3=a, SLOW=s, CHOP=c, Δslow=s - a, Δchop=c - a))
x = pd.DataFrame(rows)
if len(x):
    print(x.to_string(index=False, float_format="{:+,.0f}".format))
    print("\n  Δ집중도(CHOP): 상위3 %s" % ", ".join(
        f"{r.day} {r.Δchop:+,.0f}" for r in x.nlargest(3, "Δchop").itertuples()))
    tot = x.Δchop.sum()
    srt = x.Δchop.sort_values(ascending=False)
    for n in (1, 2, 3):
        adj = tot - srt.head(n).sum()
        print(f"    상위 {n}일 제외 : {adj:+,.0f}  {'우위' if adj>0 else '열위'}")

sec("3. 10/01~10/08")
rows = []
for d in [x for x in DAYS if x >= "20261001"]:
    rows.append(dict(day=d, P3=sum(t["krw"] for t in T3 if t["day"] == d),
                     SLOW=sum(t["krw"] for t in TS if t["day"] == d),
                     CHOP=sum(t["krw"] for t in TC if t["day"] == d)))
x = pd.DataFrame(rows)
x["Δchop"] = x.CHOP - x.P3
print(x.to_string(index=False, float_format="{:+,.0f}".format))

sec("4. 연구값(전략 E 기준) vs production 실측(P3 기준)")
print("""
  ※ 연구 CHOP-STACK 수치는 전략 E(= P3 + BRK15 + RS125 + EARLY-PASS) 위에서
     측정된 것이다. BRK15/RS125/EARLY-PASS 는 production 에 **존재하지 않는다**.
     따라서 아래 두 열은 베이스가 달라 직접 비교 대상이 아니다.
""")
res = pd.DataFrame([
    dict(항목="기준선 총손익", 연구_E기준="+809,462", production_P3기준="{:+,.0f}".format(met(DAYS, T3)["총손익"])),
    dict(항목="CHOP 총손익", 연구_E기준="+1,744,193", production_P3기준="{:+,.0f}".format(met(DAYS, TC)["총손익"])),
    dict(항목="Δ", 연구_E기준="+934,731",
         production_P3기준="{:+,.0f}".format(met(DAYS, TC)["총손익"] - met(DAYS, T3)["총손익"])),
    dict(항목="PF", 연구_E기준="1.41 -> 2.44",
         production_P3기준="%.2f -> %.2f" % (met(DAYS, T3)["PF"], met(DAYS, TC)["PF"])),
    dict(항목="MDD%", 연구_E기준="-4.97 -> -4.56",
         production_P3기준="%.2f -> %.2f" % (met(DAYS, T3)["MDD"], met(DAYS, TC)["MDD"])),
    dict(항목="거래수", 연구_E기준="49 -> 37",
         production_P3기준="%d -> %d" % (met(DAYS, T3)["거래"], met(DAYS, TC)["거래"])),
])
print(res.to_string(index=False))
