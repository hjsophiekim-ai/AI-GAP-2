"""A12 — E_lock3.5 정밀검증.
 (1) off_tp2 임계값 곡선 — E3.5 가 '위치기반 적응'인지 단순 임계값 이동인지
 (2) 위약 분포 — 구조판정이 실제로 판정력이 있는지
 (3) 변경거래 명세
"""
import pickle, statistics as st
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B4.pkl"
A._Q3_PATH = A.HERE / "_q3base4.pkl"
from common import summarize, pnl

c = A.ctx(78); D78 = c.dates; W30 = c.dates[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D78); A.save()
mb = summarize(base, D78); mb30 = summarize([t for t in base if t['date'] in S30], W30)
key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}
OUT = {"N1": base}

def show(k, ts):
    m = summarize(ts, D78); m30 = summarize([t for t in ts if t['date'] in S30], W30)
    chg = sum(1 for t in ts if key(t) in B and abs(pnl(t) - pnl(B[key(t)])) > 1e-9)
    d78 = m['compound_pct']-mb['compound_pct']; d30 = m30['compound_pct']-mb30['compound_pct']
    print(f"{k:26s} n={m['trades']:4d} 78d={m['compound_pct']:8.2f}({d78:+7.2f}) "
          f"30d={m30['compound_pct']:7.2f}({d30:+6.2f}) PF={m['pf']:.3f} "
          f"-T10={m['top10_excl_pct']:7.2f} 변경={chg:3d}", flush=True)
    return d78, d30

print("=== (1) off_tp2 임계값 곡선 (N1 의 기존 시장단위 적응성) ===", flush=True)
for v in (3.0, 3.25, 3.5, 3.75, 4.0, 4.5, 5.0, 6.0):
    k = f"off_tp2={v}"
    OUT[k] = A.run(k, c, D78, cfg={"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": v},
                   po=po, q3=q3, h50=h50); A.save(); show(k, OUT[k])

print("\n=== (2) E_lock3.5 실제 vs 위약 ===", flush=True)
SPEC = {"decide": 3.5, "strong": {}, "weak": {"close": True}}
OUT["E3.5"] = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=SPEC); A.save()
real = show("E3.5 [실제]", OUT["E3.5"])
ds = []
for seed in range(15):
    sp = dict(SPEC); sp.update({"cond": "placebo", "seed": seed, "p_weak": 0.35})
    ts = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=sp); A.save()
    OUT[f"E3.5_placebo{seed}"] = ts
    ds.append(show(f"  위약 seed={seed}", ts))
for j, tag in ((0, "78d"), (1, "30d")):
    xs = [d[j] for d in ds]
    print(f"  >> {tag}: 실제={real[j]:+.2f} 위약 평균={st.mean(xs):+.2f} "
          f"중앙값={st.median(xs):+.2f} 최대={max(xs):+.2f} 최소={min(xs):+.2f} "
          f"위약>=실제 {sum(1 for x in xs if x>=real[j])}/{len(xs)}", flush=True)

pickle.dump({"trades": OUT, "dates": D78}, open(A.HERE / "ax78_e35.pkl", "wb"))
print("저장: ax78_e35.pkl")
