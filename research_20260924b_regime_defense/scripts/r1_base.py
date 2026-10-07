"""R1 — BASE 확립 + 2회 동일성. READ-ONLY."""
import pickle, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rlib as R

ctx = R.get_ctx()
D = list(ctx.dates)
print("ctx %d일 %s~%s" % (len(D), D[0], D[-1]), flush=True)
print("9월: %s" % [d for d in D if d >= "20260901"], flush=True)

b1, m1 = R.run(ctx, D, tag="BASE (N1+C1+AR1)")
b2, m2 = R.run(ctx, D, tag="BASE 재실행")
same = R.sig(b1) == R.sig(b2)
print("\nBASE 2회 동일성:", "완전 일치" if same else "불일치!! -> 이후 수치 신뢰 불가")
assert same, "BASE 비결정적"
pickle.dump({"dates": D, "base": b1, "m": m1}, open(Path(__file__).parent / "r1.pkl", "wb"))
print("saved r1.pkl")
