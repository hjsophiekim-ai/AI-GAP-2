"""CHOP-STACK (SLOW-TP2 + 구조손절 + 2슬롯 + MC-DELAY) 집계. READ-ONLY.

9~10월 23영업일(9/3~10/08) 과 85영업일(5/27~10/01) 두 창에서 동시에 본다.
STACK2 가 돌지 않은 날은 네 부품 모두 미발동이므로 E 와 동일하다(= skip 근거).
"""
import csv, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 250)

D85 = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
D23 = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]
NEED = {l.strip() for l in open(W + "/stack2_85_need.txt") if l.strip()}


def E(d):
    for p in (f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
              f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def V(tag, d):
    p = f"{W}/out/REAL_{tag}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else E(d)


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
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


sec("0. 무결성")
have = {d for d in set(D85) | set(D23) if os.path.exists(f"{W}/out/REAL_STACK2_{d}.json")}
miss23 = [d for d in D23 if d not in have]
miss85 = [d for d in NEED if d not in have]
print("STACK2 재생 %d일 / 9~10월 미완 %s / 85일 영향일 미완 %s"
      % (len(have), miss23 or "없음", sorted(miss85) or "없음"))
print("85일 중 STACK2 영향일 %d / 나머지 %d일은 네 부품 모두 미발동 -> E 와 동일"
      % (len(NEED), 85 - len(NEED)))
TE85 = sum((trades(E(d)) for d in D85), [])
print("E 3슬롯 85일 앵커 %s (기준 +18,435,036)" % "{:+,.0f}".format(sum(t["krw"] for t in TE85)))

for lab, DD, ok in (("1. 9~10월 23영업일 (9/3~10/08)", D23, not miss23),
                    ("2. 85영업일 (5/27~10/01)", D85, not miss85)):
    if not ok:
        print("\n[%s] 미완 — 건너뜀" % lab)
        continue
    sec(lab)
    rows = []
    for nm, tag in (("E 기존 (3슬롯)", None), ("E-2SLOT", "E2SLOT"),
                    ("**CHOP-STACK**", "STACK2")):
        T = sum(((trades(E(d)) if tag is None else trades(V(tag, d))) for d in DD), [])
        rows.append(dict(변형=nm, **met(DD, T)))
    o = pd.DataFrame(rows)
    o["Δ"] = o.총손익 - o.총손익.iloc[0]
    print(o.to_string(index=False, formatters=FM))

    TS = sum((trades(V("STACK2", d)) for d in DD), [])
    TEE = sum((trades(E(d)) for d in DD), [])
    dd = pd.Series({d: sum(t["krw"] for t in TS if t["day"] == d) -
                       sum(t["krw"] for t in TEE if t["day"] == d) for d in DD})
    dd = dd[dd != 0].sort_values(ascending=False)
    print("\n  차이 발생 %d/%d일 / 합계 %s" % (len(dd), len(DD), "{:+,.0f}".format(dd.sum())))
    if len(dd):
        print("  상위3 :", ", ".join("%s %s" % (k, "{:+,.0f}".format(v)) for k, v in dd.head(3).items()))
        print("  하위3 :", ", ".join("%s %s" % (k, "{:+,.0f}".format(v)) for k, v in dd.tail(3).items()))
        tot = dd.sum()
        for n in (1, 2, 3):
            adj = tot - dd.head(n).sum()
            print("    상위 %d일 제외 : %s  %s" % (n, "{:+,.0f}".format(adj), "우위" if adj > 0 else "열위"))

sec("3. 월별 (85일)")
rows = []
for m in ("202605", "202606", "202607", "202608", "202609", "202610"):
    ddm = [d for d in D85 if d.startswith(m)]
    if not ddm:
        continue
    e = sum(t["krw"] for t in sum((trades(E(d)) for d in ddm), []))
    s = sum(t["krw"] for t in sum((trades(V("STACK2", d)) for d in ddm), []))
    rows.append(dict(월=m, 일수=len(ddm), E=e, STACK=s, Δ=s - e))
print(pd.DataFrame(rows).to_string(index=False, float_format="{:+,.0f}".format))

sec("4. 부품 발동 집계 (STACK2)")
agg = dict(MC_BLOCK=0, DAYCAP_BLOCK=0, SS_DEFER=0, SS_RECOVER=0, SS_TIMEOUT=0,
           SS_STRUCTURAL=0, SLOW_TP2_ARM=0)
for d in sorted(have):
    j = V("STACK2", d)
    for e in (j.get("brk") or []):
        if e["ev"] in agg:
            agg[e["ev"]] += 1
    for e in (j.get("ss") or []):
        if e["ev"] in agg:
            agg[e["ev"]] += 1
    agg["SLOW_TP2_ARM"] += len(j.get("slow") or [])
print(agg)

sec("5. 10/01~10/08 일별")
rows = []
for d in [x for x in D23 if x >= "20261001"]:
    rows.append(dict(day=d,
                     E=sum(t["krw"] for t in trades(E(d))),
                     E2=sum(t["krw"] for t in trades(V("E2SLOT", d))),
                     STACK=sum(t["krw"] for t in trades(V("STACK2", d)))))
x = pd.DataFrame(rows)
x["Δ"] = x.STACK - x.E
print(x.to_string(index=False, float_format="{:+,.0f}".format))
