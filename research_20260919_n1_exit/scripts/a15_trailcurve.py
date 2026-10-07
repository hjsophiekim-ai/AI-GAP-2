"""A15 — 참고: 같은 누수(peak 3.x -> 바닥 1.5 반납)를 적응규칙 없이
N1 의 trail_stop 한 값으로 막으면 어떻게 되는가. 임계값 곡선 전체 보고."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize, compound

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
mb = summarize(base, D); mb30 = summarize([t for t in base if t["date"] in S30], W30)
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}  (trail_stop=1.5)\n")
print(f"{'trail_stop':11s} {'n':>4s} {'78d':>8s} {'Δ78':>8s} {'30d':>7s} {'Δ30':>7s} {'PF':>6s} {'-T10':>7s} {'변경':>4s}")
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
for v in (1.5, 1.8, 2.0, 2.2, 2.5, 2.8, 3.0):
    ts = A.run(f"tr{v}", c, D, cfg=cfg, po={"trail_stop_pct": v, "aft_tp_pct": 4.0},
               q3=q3, h50=h50); A.save()
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    chg = sum(1 for t in ts if key(t) in B and abs(t["net_pct"]-B[key(t)]["net_pct"]) > 1e-9)
    print(f"{v:11.1f} {m['trades']:4d} {m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+8.2f} "
          f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
          f"{m['pf']:6.3f} {m['top10_excl_pct']:7.2f} {chg:4d}", flush=True)
