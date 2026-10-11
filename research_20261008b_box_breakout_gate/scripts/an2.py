"""게이트를 붙였을 때의 9월+10월 손익/PF/MDD/손실일 (산술 counterfactual, 재생 아님). READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, W)
from feat import trades  # noqa

d = pd.read_csv(W + "/feat.csv")
d["day"] = d.day.astype(str)
d["t"] = pd.to_datetime(d.dec).dt.strftime("%H:%M")

# ── 전체 거래 (표본 밖 이른아침 신호 포함) ────────────────────────────────
TR = []
for dd in [ROOT + "/research_20261008_peaklock/out", W + "/out"]:
    for p in sorted(glob.glob(dd + "/REAL_UPFASTRS_*.json")):
        day = os.path.basename(p)[-13:-5]
        if day < "20260901":
            continue
        for t in trades(json.load(open(p, encoding="utf-8"))):
            TR.append(dict(day=day, sid=t["sid"].replace(":BRK", ""), krw=t["krw"],
                           entry=str(t["entry"])[11:16], netpct=t["net"]))
TR.append(dict(day="20261008", sid="20261008_093600_DOWN_BLUE:TW2_3SLOT_CONFIRM",
               krw=-113928.0, entry="09:42", netpct=-113928 / (1510 * 6210) * 100))
TR.append(dict(day="20261008", sid="20261008_101200_UP_RED:TW2_3SLOT_CONFIRM",
               krw=-90802.0, entry="10:18", netpct=-90802 / (838 * 10361) * 100))
tr = pd.DataFrame(TR)
DAYS = sorted(set(tr.day) | set(d.day))
F = d.set_index("sid")
tr["ingate"] = tr.sid.isin(F.index)
print("E(UPFASTRS) 9월+10월 거래 %d건 / %d일" % (len(tr), len(DAYS)))
print("  게이트 평가 가능(박스 25분 이상 확보) {}건 {:+,.0f}원".format(
      int(tr.ingate.sum()), tr[tr.ingate].krw.sum()))
print("  평가 불가(09:26 이전 승인 = 박스 미형성)   {}건 {:+,.0f}원".format(
      int((~tr.ingate).sum()), tr[~tr.ingate].krw.sum()))


def metrics(days, k):
    daily = np.array([k.get(x, 0.0) for x in days], float)
    r = daily / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    return dict(krw=daily.sum(), comp=(eq[-1] - 1) * 100, mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                lossd=int((daily < 0).sum()), dmin=daily.min())


def run(name, keep_sids, sub=None):
    t = tr if sub is None else tr[tr.day.isin(sub)]
    days = DAYS if sub is None else sorted(sub)
    kept = t[t.sid.isin(keep_sids) | ~t.ingate]
    k = kept.groupby("day").krw.sum().to_dict()
    m = metrics(days, k)
    kk = kept.krw.to_numpy()
    pf = kk[kk > 0].sum() / max(-kk[kk < 0].sum(), 1.0)
    return dict(변형=name, 총손익=m["krw"], PF=pf, MDD=m["mdd"], 손실일=m["lossd"],
                거래=len(kept), 승률=100 * (kk > 0).mean() if len(kk) else 0,
                최대1일손실=m["dmin"])


ALL = set(tr.sid)
tr9, tr10 = [x for x in DAYS if x < "20261001"], [x for x in DAYS if x >= "20261001"]
GATES = {"A 기존 E (게이트 없음)": ALL}
trn = d[d.day < "20261001"]
for f, sgn in (("nover40", 1), ("peff40", 1), ("net40", 1), ("brk30", 1), ("ext3", 1)):
    for q, qn in ((0.50, "9월중앙값"), (0.3333, "9월하위1/3")):
        thr = trn[f].quantile(q if sgn > 0 else 1 - q)
        GATES["GATE %s>=%.3f (%s)" % (f, thr, qn)] = set(d[d[f] >= thr].sid)

for label, days in (("전체 9월+10월 (23일)", None), ("9월 (18일)", tr9), ("10월 (5일)", tr10)):
    print("\n" + "=" * 110 + "\n" + label + "\n" + "=" * 110)
    rows = [run(n, s, days) for n, s in GATES.items()]
    o = pd.DataFrame(rows)
    base = o.iloc[0]
    o["Δ손익"] = o.총손익 - base.총손익
    print(o.to_string(index=False, formatters={
        "총손익": lambda x: "{:+,.0f}".format(x), "Δ손익": lambda x: "{:+,.0f}".format(x),
        "최대1일손실": lambda x: "{:+,.0f}".format(x), "PF": lambda x: "%.2f" % x,
        "MDD": lambda x: "%.2f" % x, "승률": lambda x: "%.1f" % x}))

print("\n" + "=" * 110 + "\n날짜별 (게이트 없음 A / 주요 후보)\n" + "=" * 110)
sel = ["A 기존 E (게이트 없음)"] + [k for k in GATES if k.startswith("GATE nover40") or k.startswith("GATE ext3")]
rows = []
for day in ["20261001", "20261002", "20261006", "20261007", "20261008"]:
    r = {"day": day}
    for n in sel:
        kept = tr[(tr.day == day) & (tr.sid.isin(GATES[n]) | ~tr.ingate)]
        r[n.split(" (")[0]] = kept.krw.sum()
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: "{:+,.0f}".format(x)))
