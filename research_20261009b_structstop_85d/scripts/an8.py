"""B CHOP 구조손절 — 85영업일 full replay 집계. READ-ONLY.

비트리거일(= A 에 B3_SL 이 하나도 없는 날)은 monkeypatch 가 발동하지 않으므로 B = A 다.
그 주장은 대조 재생으로 검증한다(§0).
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
TRIG = [l.strip() for l in open(W + "/trigger_days.txt") if l.strip()]
BUDGET = 1e7


def A(d):
    for p in (f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def B(d):
    p = f"{W}/out/REAL_STRUCTSTOP_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else A(d)


def okey(o):
    return [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


sec("0. 무결성")
JA = {d: A(d) for d in DAYS}
JB = {d: B(d) for d in DAYS}
TA = sum((trades(JA[d]) for d in DAYS), [])
print("A 85일 총손익 %s (기준 +18,435,036) / 거래 %d" %
      ("{:+,.0f}".format(sum(t["krw"] for t in TA)), len(TA)))
ran = [d for d in DAYS if os.path.exists(f"{W}/out/REAL_STRUCTSTOP_{d}.json")]
ctl = [d for d in ran if d not in TRIG]
bad = [d for d in ctl if okey(JB[d]) != okey(JA[d])]
print("B 실제 재생 %d일 (트리거 %d/%d + 대조 %d일) / 대조일 주문 불일치 %s"
      % (len(ran), len([d for d in ran if d in TRIG]), len(TRIG), len(ctl), bad or "없음"))
miss = [d for d in TRIG if d not in ran]
print("미실행 트리거일:", miss or "없음")
TB = sum((trades(JB[d]) for d in DAYS), [])


def met(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    r = daily / BUDGET * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), 복리=(eq[-1] - 1) * 100,
                PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                최대1일손실=daily.min(), 손실일=int((daily < 0).sum()),
                승률=(k > 0).mean() * 100, 거래=len(tr))


FM = {"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "최대1일손실": "{:+,.0f}".format,
      "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format, "복리": "{:+.1f}".format}

sec("1. 85영업일 전체 (2026-05-27 ~ 2026-10-01)")
o = pd.DataFrame([dict(변형="A 기존 E", **met(DAYS, TA)),
                  dict(변형="B E+STRUCTURAL-STOP", **met(DAYS, TB))])
o["Δ"] = o.총손익 - o.총손익.iloc[0]
print(o.to_string(index=False, formatters=FM))

sec("2. train 5~7월 / test 8~10월")
tr_d = [d for d in DAYS if d < "20260801"]
te_d = [d for d in DAYS if d >= "20260801"]
for lab, dd in (("train 5~7월 (%d일)" % len(tr_d), tr_d), ("test 8~10월 (%d일)" % len(te_d), te_d)):
    x = pd.DataFrame([dict(변형="A 기존 E", **met(dd, [t for t in TA if t["day"] in dd])),
                      dict(변형="B STRUCTURAL-STOP", **met(dd, [t for t in TB if t["day"] in dd]))])
    x["Δ"] = x.총손익 - x.총손익.iloc[0]
    print("\n[%s]" % lab)
    print(x.to_string(index=False, formatters=FM))

sec("3. B3_SL 대상 건별 전/후")
ka = {(t["day"], t["sym"], str(t["entry"])): t for t in TA}
kb = {(t["day"], t["sym"], str(t["entry"])): t for t in TB}
sl = [k for k, t in ka.items() if "B3_SL" in "+".join(t["reasons"])]
rows = []
for k in sorted(sl):
    ta, tb = ka[k], kb.get(k)
    rows.append(dict(day=k[0], sym=k[1], 진입=str(ta["entry"])[11:16],
                     A청산=str(ta["exit"])[11:16], A_net=ta["net"], A_krw=ta["krw"],
                     B청산=str(tb["exit"])[11:16] if tb else "-",
                     B사유="+".join(dict.fromkeys(tb["reasons"])) if tb else "-",
                     B_net=tb["net"] if tb else np.nan, B_krw=tb["krw"] if tb else np.nan))
r = pd.DataFrame(rows)
r["Δ"] = r.B_krw - r.A_krw
print(r.to_string(index=False, float_format="{:,.2f}".format))
imp, wor = r[r["Δ"] > 0], r[r["Δ"] < 0]
print("\n  B3_SL 대상 %d건 : A %s -> B %s  (Δ %s)"
      % (len(r), "{:+,.0f}".format(r.A_krw.sum()), "{:+,.0f}".format(r.B_krw.sum()),
         "{:+,.0f}".format(r["Δ"].sum())))
print("  개선 %d건 %s / 악화 %d건 %s / 동일 %d건"
      % (len(imp), "{:+,.0f}".format(imp["Δ"].sum()), len(wor), "{:+,.0f}".format(wor["Δ"].sum()),
         len(r) - len(imp) - len(wor)))

sec("4. 유예가 만든 꼬리 — -1% 보다 깊어진 거래")
deep = r[r.B_net < -1.0]
print("  B 에서 net < -1.0%% 로 끝난 거래 %d건 / 최악 net %.2f%% (%s원, %s %s)"
      % (len(deep), deep.B_net.min() if len(deep) else 0,
         "{:+,.0f}".format(deep.B_krw.min()) if len(deep) else 0,
         deep.loc[deep.B_krw.idxmin(), "day"] if len(deep) else "-",
         deep.loc[deep.B_krw.idxmin(), "B사유"] if len(deep) else "-"))
if len(deep):
    print(deep[["day", "sym", "A_net", "B_net", "A_krw", "B_krw", "Δ", "B사유"]].to_string(
        index=False, float_format="{:,.2f}".format))

sec("5. -0.2% 회복청산으로 구제된 거래")
ev = [dict(e, day=e["t"][:10].replace("-", "")) for d in DAYS for e in (JB[d].get("ss") or [])]
cnt = pd.Series([e["ev"] for e in ev]).value_counts().to_dict() if ev else {}
print("  ss 이벤트:", cnt)
rec_days = {(e["day"]) for e in ev if e["ev"] == "SS_RECOVER"}
rec = r[r.day.isin(rec_days) & (r["Δ"] > 0)]
print("  SS_RECOVER 발생 %d건 / 해당일 개선거래 %d건 %s원"
      % (cnt.get("SS_RECOVER", 0), len(rec), "{:+,.0f}".format(rec["Δ"].sum())))
print("  (구제 = 회복청산으로 B3_SL -1%% 를 면한 건) 상세:")
for e in ev:
    if e["ev"] in ("SS_RECOVER", "SS_STRUCTURAL", "SS_TIMEOUT"):
        print("    %s %s %s net=%s wait=%s" % (e["day"], e["ev"], e["t"][11:19], e.get("net"), e.get("wait")))

sec("6. 상위 개선 1·2·3건 제외")
d_all = r["Δ"].sum()
s = r["Δ"].sort_values(ascending=False)
base = met(DAYS, TA)["총손익"]
for n in (0, 1, 2, 3):
    adj = d_all - s.head(n).sum()
    print("  상위 %d건 제외 : Δ %s  -> B 총손익 %s  (A %s)  %s"
          % (n, "{:+,.0f}".format(adj), "{:+,.0f}".format(base + adj),
             "{:+,.0f}".format(base), "우위" if adj > 0 else "열위"))

sec("7. 트리거일 일별")
rows = []
for d in TRIG:
    rows.append(dict(day=d, A=sum(t["krw"] for t in TA if t["day"] == d),
                     B=sum(t["krw"] for t in TB if t["day"] == d)))
x = pd.DataFrame(rows)
x["Δ"] = x.B - x.A
print(x.to_string(index=False, float_format="{:+,.0f}".format))
