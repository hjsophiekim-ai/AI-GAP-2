"""E-2SLOT / +MC-DELAY 85영업일 full replay 집계. READ-ONLY."""
import csv, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
SC = {r["day"]: r for r in csv.DictReader(open(W + "/mc_scan85.csv", encoding="utf-8-sig"))}
ON = {d: ((SC[d]["ON"] or "").split(".")[0], (SC[d]["OFF"] or "").split(".")[0] or "9999")
      for d in DAYS if (SC.get(d, {}).get("ON") or "")}


def E3(d):
    for p in (f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def Av(d):
    p = f"{W}/out/REAL_E2SLOT_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def Bv(d):
    p = f"{W}/out/REAL_MCDELAY_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else Av(d)


def okey(o):
    return [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


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

sec("0. 무결성")
missA = [d for d in DAYS if Av(d) is None]
print("E-2SLOT 재생 %d/85일  미완 %s" % (85 - len(missA), missA or "없음"))
print("MC 발동일 %d/85일 : %s" % (len(ON), " ".join(sorted(ON))))
ranB = [d for d in DAYS if os.path.exists(f"{W}/out/REAL_MCDELAY_{d}.json")]
missB = [d for d in ON if d not in ranB]
ctl = [d for d in ranB if d not in ON]
bad = [d for d in ctl if Av(d) and okey(Bv(d)) != okey(Av(d))]
print("MCDELAY 재생 %d일 (발동 %d/%d, 대조 %d일) / 대조일 불일치 %s / 미실행 발동일 %s"
      % (len(ranB), len([d for d in ranB if d in ON]), len(ON), len(ctl), bad or "없음", missB or "없음"))
TE = sum((trades(E3(d)) for d in DAYS), [])
print("A' 기존 E 3슬롯 앵커 : %s (기준 +18,435,036)" % "{:+,.0f}".format(sum(t["krw"] for t in TE)))
if missA:
    print("\n※ E-2SLOT 미완 — 중단"); sys.exit(0)
TA = sum((trades(Av(d)) for d in DAYS), [])
TB = sum((trades(Bv(d)) for d in DAYS), []) if not missB else None

sec("1. 85영업일 전체 (2026-05-27 ~ 2026-10-01)")
rows = [dict(변형="A' 기존 E (3슬롯)", **met(DAYS, TE)),
        dict(변형="A  E-2SLOT", **met(DAYS, TA))]
if TB is not None:
    rows.append(dict(변형="B  E-2SLOT + MC-DELAY", **met(DAYS, TB)))
o = pd.DataFrame(rows)
o["Δ(vs 3슬롯)"] = o.총손익 - o.총손익.iloc[0]
o["Δ(vs 2슬롯)"] = o.총손익 - o.총손익.iloc[1]
print(o.to_string(index=False, formatters=dict(FM, **{"Δ(vs 3슬롯)": "{:+,.0f}".format,
                                                      "Δ(vs 2슬롯)": "{:+,.0f}".format})))

sec("2. train 5~7월 / test 8~10월")
for lab, dd in (("train 5~7월", [d for d in DAYS if d < "20260801"]),
                ("test 8~10월", [d for d in DAYS if d >= "20260801"])):
    r = [dict(변형="A' 3슬롯", **met(dd, [t for t in TE if t["day"] in dd])),
         dict(변형="A  2슬롯", **met(dd, [t for t in TA if t["day"] in dd]))]
    if TB is not None:
        r.append(dict(변형="B  +MC", **met(dd, [t for t in TB if t["day"] in dd])))
    x = pd.DataFrame(r)
    x["Δ(vs 3슬롯)"] = x.총손익 - x.총손익.iloc[0]
    print("\n[%s %d일]" % (lab, len(dd)))
    print(x.to_string(index=False, formatters=dict(FM, **{"Δ(vs 3슬롯)": "{:+,.0f}".format})))

sec("3. 월별")
rows = []
for m in ("202605", "202606", "202607", "202608", "202609", "202610"):
    dd = [d for d in DAYS if d.startswith(m)]
    if not dd:
        continue
    r = dict(월=m, 일수=len(dd),
             E3=sum(t["krw"] for t in TE if t["day"] in dd),
             E2=sum(t["krw"] for t in TA if t["day"] in dd))
    r["Δ2-3"] = r["E2"] - r["E3"]
    if TB is not None:
        r["MC"] = sum(t["krw"] for t in TB if t["day"] in dd)
        r["ΔMC-2"] = r["MC"] - r["E2"]
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False, float_format="{:+,.0f}".format))

sec("4. 일별 Δ 집중도 (E-2SLOT vs 3슬롯)")
dd = pd.Series({d: sum(t["krw"] for t in TA if t["day"] == d) - sum(t["krw"] for t in TE if t["day"] == d)
                for d in DAYS})
dd = dd[dd != 0].sort_values(ascending=False)
print("차이 발생일 %d/85일 / 합계 %s" % (len(dd), "{:+,.0f}".format(dd.sum())))
print("  상위5 :", ", ".join("%s %s" % (k, "{:+,.0f}".format(v)) for k, v in dd.head(5).items()))
print("  하위5 :", ", ".join("%s %s" % (k, "{:+,.0f}".format(v)) for k, v in dd.tail(5).items()))
tot = dd.sum()
for n in (0, 1, 2, 3):
    adj = tot - dd.head(n).sum()
    print("  상위 %d일 제외 : Δ %s  %s" % (n, "{:+,.0f}".format(adj), "우위" if adj > 0 else "열위"))

if TB is not None:
    sec("5. MC-DELAY 효과 (E-2SLOT 대비)")
    d2 = pd.Series({d: sum(t["krw"] for t in TB if t["day"] == d) - sum(t["krw"] for t in TA if t["day"] == d)
                    for d in DAYS})
    d2 = d2[d2 != 0].sort_values(ascending=False)
    print("차이 발생일 %d (발동 %d일 중) / 합계 %s" % (len(d2), len(ON), "{:+,.0f}".format(d2.sum())))
    x = pd.DataFrame([dict(day=d, ON=ON[d][0], OFF=ON[d][1],
                           A=sum(t["krw"] for t in TA if t["day"] == d),
                           B=sum(t["krw"] for t in TB if t["day"] == d)) for d in sorted(ON)])
    x["Δ"] = x.B - x.A
    print(x.to_string(index=False, float_format="{:+,.0f}".format))
    t2 = d2.sum()
    for n in (0, 1, 2, 3):
        adj = t2 - d2.head(n).sum()
        print("  상위 %d일 제외 : Δ %s  %s" % (n, "{:+,.0f}".format(adj), "우위" if adj > 0 else "열위"))
    print("\n[train 5~7월] ΔMC %s / [test 8~10월] ΔMC %s"
          % ("{:+,.0f}".format(sum(v for k, v in d2.items() if k < '20260801')),
             "{:+,.0f}".format(sum(v for k, v in d2.items() if k >= '20260801'))))
