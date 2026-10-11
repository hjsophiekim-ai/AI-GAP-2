"""A8 — (E) 약하면 그 자리 이익확정 / (위약) 구조판정을 난수로 바꾼 분포.

위약: 같은 비율(35%)로 WEAK 을 찍되 구조를 보지 않는다. 후보의 우위가
'구조를 봤기 때문'인지 '바닥을 올렸기 때문(판정 무관)'인지 가른다.
"""
import sys, pickle
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B2.pkl"
A._Q3_PATH = A.HERE / "_q3base2.pkl"
from common import summarize

c = A.ctx(78); D78 = c.dates; W30 = c.dates[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D78); A.save()
mb = summarize(base, D78); mb30 = summarize([t for t in base if t['date'] in S30], W30)
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}\n", flush=True)
OUT = {"N1": base}

def show(k, ts):
    m = summarize(ts, D78); m30 = summarize([t for t in ts if t['date'] in S30], W30)
    print(f"{k:26s} n={m['trades']:4d} 78d={m['compound_pct']:8.2f} ({m['compound_pct']-mb['compound_pct']:+7.2f}) "
          f"30d={m30['compound_pct']:7.2f} ({m30['compound_pct']-mb30['compound_pct']:+6.2f}) "
          f"PF={m['pf']:.3f} -T10={m['top10_excl_pct']:7.2f}", flush=True)
    return m['compound_pct']-mb['compound_pct'], m30['compound_pct']-mb30['compound_pct']

print("=== E: 구조가 약하면 그 자리에서 전량 이익확정 ===", flush=True)
for lv in (3.0, 3.5, 4.0):
    k = f"E_lock{lv}"
    OUT[k] = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50,
                   ax={"decide": lv, "strong": {}, "weak": {"close": True}}); A.save(); show(k, OUT[k])

MECH = {
    "A2b(floor2.0@3.5)": {"decide": 3.5, "strong": {}, "weak": {"floor": 2.0}},
    "DC3W(brk C3@3.0)":  {"decide": 3.0, "strong": {}, "weak": {},
                          "brk": {"arm": 3.0, "variant": "C3", "mode": "WEAK"}},
}
res = {}
for name, spec in MECH.items():
    print(f"\n=== 위약 분포 — {name} (구조판정 → 난수, p_weak=0.35) ===", flush=True)
    real = show(f"{name} [실제]", A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=spec))
    A.save()
    ds = []
    for seed in range(12):
        sp = dict(spec); sp.update({"cond": "placebo", "seed": seed, "p_weak": 0.35})
        ts = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=sp); A.save()
        ds.append(show(f"  위약 seed={seed}", ts))
    res[name] = {"real": real, "placebo": ds}
    import statistics as st
    for j, tag in ((0, "78d"), (1, "30d")):
        xs = [d[j] for d in ds]
        better = sum(1 for x in xs if x >= real[j])
        print(f"  >> {tag}: 실제={real[j]:+.2f}  위약 평균={st.mean(xs):+.2f} "
              f"중앙값={st.median(xs):+.2f} 최대={max(xs):+.2f} "
              f"위약이 실제 이상인 횟수={better}/{len(xs)}", flush=True)
pickle.dump({"trades": OUT, "res": res}, open(A.HERE / "ax78_e_placebo.pkl", "wb"))
