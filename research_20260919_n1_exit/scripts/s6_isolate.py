"""S6 — 로버스트 성분(PP 고arm + gap_neg) 단독 분리 + arm 전구간 스캔."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B2.pkl"
A._Q3_PATH = A.HERE / "_q3base2.pkl"
from common import summarize
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
OUT = {"N1": base}
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(x for x, t in B.items() if t["peak_net_pct"] >= 8.0)

def go(k, ax):
    OUT[k] = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, ax=ax); A.save()
    ts = OUT[k]; r = V.report(k, ts, base, D); m = summarize(ts, D)
    ka = {key(t): t for t in ts}
    dmg = sum(ka[x]["net_pct"]-B[x]["net_pct"] for x in RUN8 if x in ka)
    print(f"{k:26s} n={m['trades']:4d} Δ78={r['78d_d']:+7.2f} Δ30={r['30d_d']:+6.2f} "
          f"PF={m['pf']:6.3f}(Δ{m['pf']-2.5868:+.3f}) PF30={r['30d_pf_a']:.3f} MDD={m['mdd_pct']:6.2f} "
          f"ΔT10={r['top10_d']:+6.2f}/{r['top10_d30']:+5.2f} 앞={r['half'][0]:+6.2f} 뒤={r['half'][1]:+6.2f} "
          f"WF={r['wf6_win']}/{r['wf6_lose']} 변경={r['chg']:3d}({r['up']}↑{r['dn']}↓) "
          f"Σ={r['sum_d']:+6.2f} Σ-3={r['sum_d_ex3']:+6.2f} run8={dmg:+5.2f}", flush=True)

NC = {"decide": 99.0, "strong": {}, "weak": {}}   # E 성분 없음
print("=== PP 단독 (E3.5 없음) — arm 스캔, give 1.5, gap_neg ===", flush=True)
for arm in (3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5):
    go(f"PPonly_a{arm}_g1.5", dict(NC, pp={"arm": arm, "give": 1.5, "cond": "gap_neg"}))
print("\n=== PP 단독 arm 5.0 — give 스캔 ===", flush=True)
for g in (1.0, 1.5, 2.0, 2.5, 3.0):
    go(f"PPonly_a5.0_g{g}", dict(NC, pp={"arm": 5.0, "give": g, "cond": "gap_neg"}))
print("\n=== PP 단독 arm 5.0 — 조건 대체 (조건이 정말 필요한가) ===", flush=True)
for cd in ("gap_neg", "watch", "gap_shrink", "regime_off", "vwap_off", None):
    go(f"PPonly_a5.0_{cd or 'NONE'}",
       dict(NC, pp={"arm": 5.0, "give": 1.5, **({"cond": cd} if cd else {})}))
print("\n=== 참고: E3.5 결합판 ===", flush=True)
for arm in (4.5, 5.0, 5.5):
    go(f"E35+PP_a{arm}", {"decide": 3.5, "strong": {}, "weak": {"close": True},
                          "pp": {"arm": arm, "give": 1.5, "cond": "gap_neg"}})
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "s6.pkl", "wb"))
print("\n저장: s6.pkl")
