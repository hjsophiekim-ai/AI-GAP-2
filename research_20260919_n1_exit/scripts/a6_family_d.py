"""A6 — D계열(구조붕괴 청산) + N1 기존 적응성 기준선."""
import time, pickle
import axlib as A
from axdefs import AX
from common import summarize

c = A.ctx(78); D78 = c.dates; D30 = set(c.dates[-30:])
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D78); A.save()
mb, mb30 = summarize(base, D78), summarize([t for t in base if t['date'] in D30], c.dates[-30:])
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}", flush=True)

REF = {  # N1 의 기존(시장단위) 적응성이 얼마나 하고 있는지
    "ref_no_offtp2": ({"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0}, po),
    "ref_off_3.0":   ({"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 3.0}, po),
    "ref_off_5.0":   ({"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 5.0}, po),
    "ref_tp2_4_flat":({"tp1": 3.5, "tp2": 4.0, "tp1_ratio": 0.0, "off_tp2": 4.0}, po),
}
OUT = {"N1": base}
print(f"\n{'변형':22s} {'n':>4s} {'78d':>8s} {'Δ':>7s} {'30d':>7s} {'Δ':>7s} {'PF':>6s} {'MDD':>7s} {'-T10':>7s}")
def show(k, ts):
    m = summarize(ts, D78); m30 = summarize([t for t in ts if t['date'] in D30], c.dates[-30:])
    print(f"{k:22s} {m['trades']:4d} {m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+7.2f} "
          f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:7.2f}", flush=True)
show("N1", base)
for k, (cf, pp) in REF.items():
    OUT[k] = A.run(k, c, D78, cfg=cf, po=pp, q3=q3, h50=h50); A.save(); show(k, OUT[k])
for k in [x for x in AX if x.startswith("D_")]:
    OUT[k] = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=AX[k]); A.save(); show(k, OUT[k])
pickle.dump({"trades": OUT, "dates": D78}, open(A.HERE / "ax78_d.pkl", "wb"))
