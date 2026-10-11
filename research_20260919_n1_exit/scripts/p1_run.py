"""P1 — peak-relative profit protection 후보 전량 실행.

후보는 실행 전에 확정했다(사후 선택 없음):
  계열1 peak drawdown 6 / 계열2 profit floor 4 / 계열3 결합 10 (베이스 2 x 조건 5)
베이스 2개는 저장소 상수로 미리 고른 것:
  PD a3.5/g1.5  (3.5=MORNING_TRAILING_TRIGGER, 1.5=N1 trail_stop)
  PF a4.0/f3.0  (4.0=N1 off_tp2/오후TP, 3.0=MORNING_TP1)
"""
import pickle, time
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
mb = summarize(base, D); mb30 = summarize([t for t in base if t["date"] in S30], W30)
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}\n", flush=True)

CAND = {}
# 계열 1 — peak drawdown
for arm, give in ((3.0, 1.0), (3.5, 1.0), (3.5, 1.5), (4.0, 1.0), (4.0, 1.5), (4.0, 2.0)):
    CAND[f"PD_a{arm}_g{give}"] = {"decide": 99.0, "strong": {}, "weak": {},
                                  "pp": {"arm": arm, "give": give}}
# 계열 2 — profit floor
for arm, flr in ((3.5, 2.5), (4.0, 3.0), (5.0, 3.5), (5.0, 4.0)):
    CAND[f"PF_a{arm}_f{flr}"] = {"decide": 99.0, "strong": {}, "weak": {},
                                 "pp": {"arm": arm, "floor": flr}}
# 계열 3 — 구조조건 결합
BASES = {"PDb": {"arm": 3.5, "give": 1.5}, "PFb": {"arm": 4.0, "floor": 3.0}}
CONDS = ("regime_off", "gap_shrink", "gap_neg", "vwap_off", "watch")
for bn, bs in BASES.items():
    for cd in CONDS:
        CAND[f"{bn}+{cd}"] = {"decide": 99.0, "strong": {}, "weak": {},
                              "pp": dict(bs, cond=cd)}

OUT = {"N1": base, "N1_Safe": A.run("N1_Safe", c, D)}
A.save()
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = {k for k, t in B.items() if t["peak_net_pct"] >= 8.0}

print(f"{'후보':20s} {'n':>4s} {'78d':>8s} {'Δ78':>8s} {'30d':>7s} {'Δ30':>7s} "
      f"{'PF':>6s} {'MDD':>7s} {'-T10':>7s} {'변경':>4s} {'발동':>4s} {'run8손상':>8s}")
for k, sp in CAND.items():
    t0 = time.time()
    ts = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, ax=sp)
    OUT[k] = ts; A.save()
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    ka = {key(t): t for t in ts}
    chg = sum(1 for kk in set(ka) & set(B) if abs(ka[kk]["net_pct"] - B[kk]["net_pct"]) > 1e-9)
    fired = sum(1 for t in ts if t.get("pp_fired"))
    dmg = sum(ka[kk]["net_pct"] - B[kk]["net_pct"] for kk in RUN8 if kk in ka)
    print(f"{k:20s} {m['trades']:4d} {m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+8.2f} "
          f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:7.2f} {chg:4d} {fired:4d} "
          f"{dmg:+8.2f}  {time.time()-t0:.0f}s", flush=True)
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "pp78.pkl", "wb"))
print("\n저장: pp78.pkl")
