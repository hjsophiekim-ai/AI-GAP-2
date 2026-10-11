"""A PROBE->PROMOTE / B CHOP 구조손절 vs 기존 E — 9~10월 REAL 재생 집계. READ-ONLY."""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 240)

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]
NM = {"A": "기존 E (P3/3슬롯)", "PROBE": "A PROBE→PROMOTE", "STRUCTSTOP": "B CHOP 구조손절"}


def ld(v, d):
    ps = ([f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
           f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"] if v == "A" else [f"{W}/out/REAL_{v}_{d}.json"])
    for p in ps:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


VARS = [v for v in ("A", "PROBE", "STRUCTSTOP")
        if all(ld(v, d) is not None for d in DAYS)]
print("완주 변형:", [NM[v] for v in VARS], "/ %d영업일" % len(DAYS))
T = {v: sum((trades(ld(v, d)) for d in DAYS), []) for v in VARS}


def met(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    r = daily / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                최대1일손실=daily.min(), 손실일=int((daily < 0).sum()),
                승률=(k > 0).mean() * 100, 거래=len(tr),
                평균보유=np.mean([t["hold_min"] for t in tr]) if tr else 0.0)


def sec(t):
    print("\n" + "=" * 110 + "\n" + t + "\n" + "=" * 110)


FM = {"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "최대1일손실": "{:+,.0f}".format,
      "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format,
      "평균보유": "{:.0f}".format}

sec("1. 전체 9월+10월 (%d영업일)" % len(DAYS))
o = pd.DataFrame([dict(변형=NM[v], **met(DAYS, T[v])) for v in VARS])
o["Δ"] = o.총손익 - o.총손익.iloc[0]
print(o.to_string(index=False, formatters=FM))

sec("2. 9월(18일) / 10월(5일)")
for lab, dd in (("9월", [d for d in DAYS if d < "20261001"]), ("10월", [d for d in DAYS if d >= "20261001"])):
    x = pd.DataFrame([dict(변형=NM[v], **met(dd, [t for t in T[v] if t["day"] in dd])) for v in VARS])
    x["Δ"] = x.총손익 - x.총손익.iloc[0]
    print("\n[%s]" % lab)
    print(x.to_string(index=False, formatters=FM))

sec("3. A PROBE→PROMOTE 추가진입 성공/실패")
if "PROBE" in VARS:
    ev = [e for d in DAYS for e in (ld("PROBE", d).get("pr") or [])]
    c = pd.Series([e["ev"] for e in ev]).value_counts().to_dict()
    print("  이벤트:", c)
    pro = [e for e in ev if e["ev"] == "PROBE_PROMOTE" and e.get("done")]
    print("  probe 진입 %d건 중 승격 %d건 (%.0f%%) / 만료 %d / 박스복귀 취소 %d / 승격 전 청산 %d"
          % (c.get("PROBE_IN", 0), len(pro), 100 * len(pro) / max(c.get("PROBE_IN", 0), 1),
             c.get("PROBE_EXPIRE", 0), c.get("PROBE_ABORT_BOX", 0), c.get("PROBE_CLOSED", 0)))
    if pro:
        print("  승격 지연 평균 %.2f분 / 중앙 %.2f분" %
              (np.mean([e["delay_min"] for e in pro]), np.median([e["delay_min"] for e in pro])))
    # 승격 성공/실패를 거래 결과로
    pk = {(e["t"][:10].replace("-", ""), e["sym"]) for e in pro}
    w = [t for t in T["PROBE"] if (t["day"], t["sym"]) in pk]
    nw = [t for t in T["PROBE"] if (t["day"], t["sym"]) not in pk]
    print("  승격한 거래 %d건 %s원 (승률 %.0f%%) / 승격 못한 거래 %d건 %s원 (승률 %.0f%%)"
          % (len(w), "{:+,.0f}".format(sum(t["krw"] for t in w)),
             100 * np.mean([t["krw"] > 0 for t in w]) if w else 0,
             len(nw), "{:+,.0f}".format(sum(t["krw"] for t in nw)),
             100 * np.mean([t["krw"] > 0 for t in nw]) if nw else 0))

sec("4. B 구조손절 — -1% 손절을 피한 건수와 최종 결과")
if "STRUCTSTOP" in VARS:
    ev = [dict(e, day=e["t"][:10].replace("-", "")) for d in DAYS for e in (ld("STRUCTSTOP", d).get("ss") or [])]
    c = pd.Series([e["ev"] for e in ev]).value_counts().to_dict()
    print("  이벤트:", c)
    asl = {(t["day"], t["sym"]): t for t in T["A"] if "B3_SL" in "+".join(t["reasons"])}
    bmap = {(t["day"], t["sym"]): t for t in T["STRUCTSTOP"]}
    rows = []
    for k, ta in sorted(asl.items()):
        tb = bmap.get(k)
        rows.append(dict(day=k[0], sym=k[1], A진입=str(ta["entry"])[11:16], A청산=str(ta["exit"])[11:16],
                         A_net=ta["net"], A_krw=ta["krw"],
                         B청산=str(tb["exit"])[11:16] if tb else "-",
                         B사유="+".join(dict.fromkeys(tb["reasons"])) if tb else "-",
                         B_net=tb["net"] if tb else np.nan, B_krw=tb["krw"] if tb else np.nan))
    r = pd.DataFrame(rows)
    r["Δ"] = r.B_krw - r.A_krw
    print(r.to_string(index=False, float_format="{:,.2f}".format))
    print("\n  A 의 B3_SL %d건 합계 %s원 -> B 에서 %s원 (Δ %s)"
          % (len(r), "{:+,.0f}".format(r.A_krw.sum()), "{:+,.0f}".format(r.B_krw.sum()),
             "{:+,.0f}".format(r["Δ"].sum())))
    print("  개선 %d건 %s / 악화 %d건 %s"
          % (int((r["Δ"] > 0).sum()), "{:+,.0f}".format(r.loc[r["Δ"] > 0, "Δ"].sum()),
             int((r["Δ"] < 0).sum()), "{:+,.0f}".format(r.loc[r["Δ"] < 0, "Δ"].sum())))

sec("5. 10/01~10/08 일별")
rows = []
for d in [x for x in DAYS if x >= "20261001"]:
    rows.append(dict(day=d, **{NM[v]: sum(t["krw"] for t in T[v] if t["day"] == d) for v in VARS}))
print(pd.DataFrame(rows).to_string(index=False, float_format="{:+,.0f}".format))

sec("6. 전체 일별")
rows = []
for d in DAYS:
    rows.append(dict(day=d, **{NM[v]: sum(t["krw"] for t in T[v] if t["day"] == d) for v in VARS}))
print(pd.DataFrame(rows).to_string(index=False, float_format="{:+,.0f}".format))
