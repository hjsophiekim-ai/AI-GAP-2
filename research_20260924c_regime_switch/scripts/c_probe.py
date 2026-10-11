import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
import hengine5 as H
H._CTX_CACHE = Path(__file__).resolve().parent / "_ctx_B.pkl"
import cxlib as CX
import numpy as np
ctx = H.build_ctx(78)
D = list(ctx.dates); TRAIN = {d for d in D if d < "20260901"}
F = CX.build(ctx.hynix_bars_3m, ctx.flags_by_idx, TRAIN)
print("q:", F["q"])
for strat, par in (("C1", {}), ("C2", {}), ("C3", {"qtl": 80}), ("C3", {"qtl": 85})):
    hits = [i for i in range(len(F["close"])) if CX.evaluate(strat, F, i, par) is not None]
    sep = [i for i in hits if F["date"][i] >= "20260901"]
    print("%s%s 트리거 %4d건 (9월 %3d건 / 비9월 %4d건) · 9월비중 %.1f%%"
          % (strat, par, len(hits), len(sep), len(hits) - len(sep),
             100 * len(sep) / max(1, len(hits))))
