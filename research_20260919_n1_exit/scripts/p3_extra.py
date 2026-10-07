"""P3 — (a) 목표 3건을 실제로 살리는 설정 2개를 결합조건으로 좁혀본다
         (계열3 에서 유일하게 구제가 나온 regime_off 의 give 1.0 / arm 3.0 판)
       (b) 구제·훼손 거래 전량 덤프."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
P = pickle.load(open(A.HERE / "pp78.pkl", "rb"))
TR = P["trades"]; base = TR["N1"]
mb = summarize(base, D); mb30 = summarize([t for t in base if t["date"] in S30], W30)
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
RESCUE = {"20260803", "20260827", "20260831"}
KEEP = {"20260527", "20260624", "20260703", "20260706", "20260714", "20260731"}

EXTRA = {
    "PD_a3.5_g1.0+regime_off": {"arm": 3.5, "give": 1.0, "cond": "regime_off"},
    "PD_a3.0_g1.0+regime_off": {"arm": 3.0, "give": 1.0, "cond": "regime_off"},
}
for k, pp in EXTRA.items():
    TR[k] = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50,
                  ax={"decide": 99.0, "strong": {}, "weak": {}, "pp": pp})
    A.save()
pickle.dump({"trades": TR, "dates": D}, open(A.HERE / "pp78b.pkl", "wb"))

SHOW = ["PD_a3.5_g1.0+regime_off", "PD_a3.0_g1.0+regime_off", "PDb+regime_off",
        "PD_a3.5_g1.0", "PDb+gap_neg", "PD_a4.0_g2.0", "PF_a5.0_f4.0"]
print(f"{'후보':26s} {'n':>4s} {'Δ78':>8s} {'Δ30':>7s} {'앞39':>7s} {'뒤39':>7s} {'WF':>6s} "
      f"{'ΔT10':>7s} {'변경':>4s} {'Σ단순':>7s} {'Σ-3':>7s} {'구제3':>6s} {'유지6잘림':>8s} {'runner':>7s}")
for k in SHOW:
    ts = TR[k]; ka = {key(t): t for t in ts}
    r = V.report(k, ts, base, D)
    m = summarize(ts, D)
    d = lambda kk: ka[kk]["net_pct"] - B[kk]["net_pct"]
    resc = sum(d(kk) for kk in B if kk[0] in RESCUE and kk in ka)
    nres = sum(1 for kk in B if kk[0] in RESCUE and kk in ka and d(kk) > 0.01)
    cut = sum(1 for kk in B if kk[0] in KEEP and kk in ka and d(kk) < -0.01)
    dmg = sum(d(kk) for kk in RUN8 if kk in ka)
    print(f"{k:26s} {m['trades']:4d} {r['78d_d']:+8.2f} {r['30d_d']:+7.2f} "
          f"{r['half'][0]:+7.2f} {r['half'][1]:+7.2f} {r['wf6_win']:2d}/{r['wf6_lose']:<2d} "
          f"{r['top10_d']:+7.2f} {r['chg']:4d} {r['sum_d']:+7.2f} {r['sum_d_ex3']:+7.2f} "
          f"{resc:+6.2f}({nres}) {cut:8d} {dmg:+7.2f}")

for k in SHOW[:5]:
    ts = TR[k]; ka = {key(t): t for t in ts}
    r = V.report(k, ts, base, D)
    print(f"\n=== {k} — 개선/악화 거래 전량 ({r['chg']}건, 진입집합 변화 "
          f"+{r['only_a']}/-{r['only_b']}) ===")
    for kk, x in r["diffs"]:
        b_, a_ = B[kk], ka[kk]
        tag = "구제" if kk[0] in RESCUE else ("유지6" if kk[0] in KEEP else
              ("RUNNER" if kk in RUN8 else ""))
        print(f"  {kk[0]} {str(kk[1])[11:16]} peak={b_['peak_net_pct']:5.2f} "
              f"N1 {b_['net_pct']:+7.3f}[{str(b_['exit_reason'])[:22]:22s}] -> "
              f"{a_['net_pct']:+7.3f}[{str(a_['exit_reason'])[:14]:14s}] Δ{x:+7.3f} {tag}")
