"""A2 — (1) 패치엔진 앵커 재확인 (2) N1 78일 milestone 데이터셋 생성."""
import pickle, time
import axlib as A
from common import summarize

c = A.ctx(78)
print(f"ctx {len(c.dates)}일 {c.dates[0]}~{c.dates[-1]}", flush=True)
pres = pickle.load(open(A.HERE / "_ctx76_preserved.pkl", "rb"))
W70 = pres["dates"][-70:]
EXP = {"X2lite_W1": (144, 176.3247), "H50": (141, 204.1759),
       "N1": (142, 396.6787), "N1_Safe": (142, 384.4773)}

print("\n=== 앵커 재확인 (milestone 훅 추가 후) ===", flush=True)
ok = True
for k in A.SPEC:
    t = time.time()
    tr = A.run(k, c, W70)
    m = summarize(tr, W70)
    n_e, c_e = EXP[k]
    d = m["compound_pct"] - c_e
    good = m["trades"] == n_e and abs(d) < 0.005
    ok &= good
    print(f"  {k:12s} n={m['trades']:4d} 복리={m['compound_pct']:10.4f} 차이={d:+.4f} "
          f"{'OK' if good else '<<<불일치'} {time.time()-t:.0f}s", flush=True)
print("앵커:", "전부 일치" if ok else "불일치")
A.save()

print("\n=== 78일 N1 + milestone ===", flush=True)
ms = []
t = time.time()
trN1 = A.run("N1", c, c.dates, milestones=ms)
m = summarize(trN1, c.dates)
print(f"  N1 78일 n={m['trades']} 복리={m['compound_pct']:.4f} (공표 401.09) "
      f"milestone {len(ms)}건 {time.time()-t:.0f}s", flush=True)
A.save()
pickle.dump({"trades": trN1, "ms": ms, "dates": c.dates},
            open(A.HERE / "ms78_N1.pkl", "wb"))
print("저장: ms78_N1.pkl")
