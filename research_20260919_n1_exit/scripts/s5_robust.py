"""S5 — X1 강건성: (a) 조건 대체 (b) 임계값 민감도 (c) 위약(구조판정 난수화)."""
import pickle, time, sys, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
S4 = pickle.load(open(A.HERE / "s4.pkl", "rb")); OUT = dict(S4["trades"])
base = OUT["N1"]; mb = summarize(base, D)
mb30 = summarize([t for t in base if t["date"] in S30], W30)

def E(lv): return {"decide": lv, "strong": {}, "weak": {"close": True}}
def go(k, ax):
    if k in OUT:
        return
    OUT[k] = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, ax=ax); A.save()

def line(k):
    ts = OUT[k]; r = V.report(k, ts, base, D); m = summarize(ts, D)
    print(f"{k:28s} n={m['trades']:4d} Δ78={r['78d_d']:+8.2f} Δ30={r['30d_d']:+7.2f} "
          f"PF={m['pf']:6.3f} MDD={m['mdd_pct']:6.2f} 앞={r['half'][0]:+7.2f} 뒤={r['half'][1]:+7.2f} "
          f"WF={r['wf6_win']}/{r['wf6_lose']} 변경={r['chg']:3d} Σ={r['sum_d']:+6.2f} "
          f"Σ-3={r['sum_d_ex3']:+6.2f}", flush=True)
    return r

print("=== (a) pp 조건 대체 (arm 3.5 / give 1.5 고정, E3.5 위에) ===", flush=True)
for cd in ("gap_neg", "gap_shrink", "vwap_off", "watch", "regime_off", None):
    k = f"E35+pp3.5g1.5_{cd or 'NONE'}"
    go(k, dict(E(3.5), pp={"arm": 3.5, "give": 1.5, **({"cond": cd} if cd else {})}))
    line(k)

print("\n=== (b) 임계값 민감도 ===", flush=True)
print("  arm 곡선 (give 1.5, gap_neg):", flush=True)
for arm in (3.0, 3.5, 4.0, 4.5, 5.0):
    k = f"E35+gapneg_a{arm}"
    go(k, dict(E(3.5), pp={"arm": arm, "give": 1.5, "cond": "gap_neg"})); line(k)
print("  give 곡선 (arm 3.5, gap_neg):", flush=True)
for g in (1.0, 1.5, 2.0, 2.5):
    k = f"E35+gapneg_g{g}"
    go(k, dict(E(3.5), pp={"arm": 3.5, "give": g, "cond": "gap_neg"})); line(k)
print("  E 확정레벨 곡선 (pp 3.5/1.5/gap_neg):", flush=True)
for lv in (3.0, 3.5, 4.0, 5.0):
    k = f"E{lv}+gapneg"
    go(k, dict(E(lv), pp={"arm": 3.5, "give": 1.5, "cond": "gap_neg"})); line(k)

print("\n=== (c) 위약 — E 의 구조판정만 난수화 (pp 는 실제 gap_neg 유지) ===", flush=True)
real = V.report("X1", OUT["X1_E35+gapneg"], base, D)
print(f"  실제 X1: Δ78={real['78d_d']:+.2f} Δ30={real['30d_d']:+.2f} "
      f"앞={real['half'][0]:+.2f} 뒤={real['half'][1]:+.2f} WF={real['wf6_win']}/{real['wf6_lose']}")
ds = []
for seed in range(10):
    k = f"X1_placebo{seed}"
    go(k, dict(E(3.5), cond="placebo", seed=seed, p_weak=0.35,
               pp={"arm": 3.5, "give": 1.5, "cond": "gap_neg"}))
    r = line(k); ds.append((r["78d_d"], r["30d_d"]))
for j, tag in ((0, "78d"), (1, "30d")):
    xs = [x[j] for x in ds]; rv = real[f"{tag}_d"]
    print(f"  >> {tag}: 실제={rv:+.2f} 위약 평균={st.mean(xs):+.2f} 최대={max(xs):+.2f} "
          f"최소={min(xs):+.2f} 위약>=실제 {sum(1 for x in xs if x >= rv)}/{len(xs)}", flush=True)
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "s5.pkl", "wb"))
print("저장: s5.pkl")
