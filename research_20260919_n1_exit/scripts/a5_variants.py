"""A5 — AX 후보 전량 실행 (78일 + 30일). ax=None 이면 N1 과 완전동일해야 한다."""
import sys, time, pickle
import axlib as A
from axdefs import AX
from common import summarize, line

c = A.ctx(78)
D78, D30 = c.dates, c.dates[-30:]

print("=== 무개입 검증: ax=None == N1 ===", flush=True)
t = time.time(); base78 = A.run("N1", c, D78); A.save()
m = summarize(base78, D78)
print(f"  N1 78일 n={m['trades']} 복리={m['compound_pct']:.4f} (기대 401.0853) "
      f"{'OK' if abs(m['compound_pct']-401.0853)<0.005 else '<<<불일치'} {time.time()-t:.0f}s", flush=True)

OUT = {"N1": base78}
for k, spec in AX.items():
    t = time.time()
    cfg, po, q3, h50 = A.SPEC["N1"]
    OUT[k] = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=spec)
    m = summarize(OUT[k], D78)
    print(f"  {k:18s} {line('', m)[24:]}  {time.time()-t:.0f}s", flush=True)
    A.save()

pickle.dump({"trades": OUT, "dates": D78}, open(A.HERE / "ax78.pkl", "wb"))

print("\n=== 78일 요약 ===")
print(f"{'전략':20s} {'n':>4s} {'복리':>9s} {'ΔN1':>8s} {'PF':>6s} {'MDD':>7s} {'-T10':>8s} {'승률':>5s}")
mb = summarize(base78, D78)
for k, ts in OUT.items():
    m = summarize(ts, D78)
    print(f"{k:20s} {m['trades']:4d} {m['compound_pct']:9.2f} {m['compound_pct']-mb['compound_pct']:+8.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:8.2f} {m['win_rate_pct']:5.1f}")

print("\n=== 30일 (같은 거래에서 창만 잘라 집계) ===")
print(f"{'전략':20s} {'n':>4s} {'복리':>9s} {'ΔN1':>8s} {'PF':>6s} {'MDD':>7s} {'-T10':>8s}")
mb30 = summarize([t for t in base78 if t['date'] in set(D30)], D30)
for k, ts in OUT.items():
    m = summarize([t for t in ts if t['date'] in set(D30)], D30)
    print(f"{k:20s} {m['trades']:4d} {m['compound_pct']:9.2f} {m['compound_pct']-mb30['compound_pct']:+8.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:8.2f}")
