"""MORNING-CHOP-DELAY : A 기존 E-2SLOT vs B E-2SLOT+MC. 9월+10월 23영업일. READ-ONLY."""
import csv, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]
SCAN = {r["day"]: r for r in csv.DictReader(open(W + "/mc_scan.csv", encoding="utf-8-sig"))}
ON = {d: ((SCAN[d]["ON"] or "").split(".")[0], (SCAN[d]["OFF"] or "").split(".")[0])
      for d in DAYS if (SCAN.get(d, {}).get("ON") or "")}


def E3(d):
    for p in (f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
              f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
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


miss = [d for d in DAYS if Av(d) is None]
sec("0. 무결성")
print("E2SLOT 재생 %d/%d일  (미완 %s)" % (len(DAYS) - len(miss), len(DAYS), miss or "없음"))
print("MORNING_CHOP 발동일 %d일 : %s" % (len(ON), " ".join(sorted(ON))))
ctl = [d for d in DAYS if d not in ON and os.path.exists(f"{W}/out/REAL_MCDELAY_{d}.json")]
bad = [d for d in ctl if Av(d) and okey(Bv(d)) != okey(Av(d))]
print("비발동 대조일 %d일 중 A 와 주문 불일치 : %s" % (len(ctl), bad or "없음"))
if miss:
    print("\n※ E2SLOT 미완 — 아래 집계는 완료분만")
    sys.exit(0)

TE = sum((trades(E3(d)) for d in DAYS), [])
TA = sum((trades(Av(d)) for d in DAYS), [])
TB = sum((trades(Bv(d)) for d in DAYS), [])


def met(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    r = daily / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                최대1일손실=daily.min(), 손실일=int((daily < 0).sum()),
                승률=(k > 0).mean() * 100, 거래=len(tr))


FM = {"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "최대1일손실": "{:+,.0f}".format,
      "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format}

sec("1. 전체 9월+10월 (23영업일)")
o = pd.DataFrame([dict(변형="(참고) 기존 E 3슬롯", **met(DAYS, TE)),
                  dict(변형="A 기존 E-2SLOT", **met(DAYS, TA)),
                  dict(변형="B E-2SLOT + MORNING-CHOP-DELAY", **met(DAYS, TB))])
o["Δ"] = o.총손익 - o.총손익.iloc[1]        # A 기준
print(o.to_string(index=False, formatters=FM))

sec("2. 9월 / 10월")
for lab, dd in (("9월(18일)", [d for d in DAYS if d < "20261001"]),
                ("10월(5일)", [d for d in DAYS if d >= "20261001"])):
    x = pd.DataFrame([dict(변형="A E-2SLOT", **met(dd, [t for t in TA if t["day"] in dd])),
                      dict(변형="B +MC-DELAY", **met(dd, [t for t in TB if t["day"] in dd]))])
    x["Δ"] = x.총손익 - x.총손익.iloc[0]
    print("\n[%s]" % lab)
    print(x.to_string(index=False, formatters=FM))

sec("3. MC 창에서 막은 것 / 놓친 것")
blocked, after = [], []
for d in ON:
    on, off = ON[d]
    for t in trades(Av(d)):
        hm = pd.Timestamp(t["entry"]).strftime("%H%M")
        if on <= hm < (off or "9999"):
            blocked.append(t)
    for t in trades(Bv(d)):
        if pd.Timestamp(t["entry"]).strftime("%H%M") >= (off or "9999"):
            after.append(t)
bl = pd.DataFrame([dict(day=t["day"], t=str(t["entry"])[11:16], net=t["net"], krw=t["krw"]) for t in blocked])
print("A 가 MC 창 안에서 했던 진입 %d건" % len(bl))
if len(bl):
    print("  막은 오전 손실 합계 : %s (%d건)" %
          ("{:+,.0f}".format(bl.loc[bl.krw < 0, "krw"].sum()), int((bl.krw < 0).sum())))
    print("  놓친 오전 수익 합계 : %s (%d건)" %
          ("{:+,.0f}".format(bl.loc[bl.krw > 0, "krw"].sum()), int((bl.krw > 0).sum())))
    print("  순합계             : %s" % "{:+,.0f}".format(bl.krw.sum()))
    print(bl.to_string(index=False, float_format="{:,.2f}".format))
af = pd.DataFrame([dict(day=t["day"], t=str(t["entry"])[11:16], net=t["net"], krw=t["krw"]) for t in after])
print("\n해제 후 진입한 거래 %d건 / 손익 %s (승률 %.0f%%)" %
      (len(af), "{:+,.0f}".format(af.krw.sum()) if len(af) else "+0",
       100 * (af.krw > 0).mean() if len(af) else 0))
if len(af):
    print(af.to_string(index=False, float_format="{:,.2f}".format))
mb = sum(len([e for e in (Bv(d).get("brk") or []) if e["ev"] == "MC_BLOCK"]) for d in ON)
print("\nMC_BLOCK 이벤트 총 %d건" % mb)

sec("4. 10/01 ~ 10/08 일별")
rows = []
for d in [x for x in DAYS if x >= "20261001"]:
    rows.append(dict(day=d, ON=ON.get(d, ("", ""))[0] or "-", OFF=ON.get(d, ("", ""))[1] or "-",
                     A=sum(t["krw"] for t in TA if t["day"] == d),
                     B=sum(t["krw"] for t in TB if t["day"] == d)))
x = pd.DataFrame(rows)
x["Δ"] = x.B - x.A
print(x.to_string(index=False, float_format="{:+,.0f}".format))

sec("5. 발동일 일별")
rows = []
for d in sorted(ON):
    rows.append(dict(day=d, ON=ON[d][0], OFF=ON[d][1],
                     A=sum(t["krw"] for t in TA if t["day"] == d),
                     B=sum(t["krw"] for t in TB if t["day"] == d)))
x = pd.DataFrame(rows)
x["Δ"] = x.B - x.A
print(x.to_string(index=False, float_format="{:+,.0f}".format))
print("\n발동일 합계 Δ %s" % "{:+,.0f}".format(x["Δ"].sum()))

sec("6. 2026-10-08 상세")
for nm, j in (("A E-2SLOT", Av("20261008")), ("B +MC-DELAY", Bv("20261008"))):
    print("\n[%s]" % nm)
    for t in trades(j):
        print("   진입 %s %s %d주 -> %s  net %+.2f%% %s" %
              (str(t["entry"])[11:16], t["sym"], t["q"], "+".join(dict.fromkeys(t["reasons"])),
               t["net"], "{:+,.0f}".format(t["krw"])))
    print("   신호: " + " / ".join(
        "%s %s %s" % (s["completed_bar_at"][:4], s["signal_id"].split("_")[-1][:12],
                      s.get("block_reason") or s["order_result"])
        for s in j["sig"] if ":" in s["signal_id"]))
