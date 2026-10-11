import numpy as np, random
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B3.pkl"
import hengine5 as H, tregime as tr
from app.trading.macd2.models import Direction
c = A.ctx(78); bars = c.hynix_bars_3m
tbl = H.snap_table(bars)
random.seed(0)
bad = 0; n = 0
for i in random.sample(range(60, len(bars)), 250):
    for d in (Direction.UP_RED, Direction.DOWN_BLUE):
        for v in ("C1", "C2", "C3", "C4"):
            for cnt in (0, 1, 2):
                a = tr.should_release(bars.iloc[: i + 1], d, v, cnt)
                b = H.fast_release(tbl, i, d, v, cnt)
                n += 1
                if a != b:
                    bad += 1
                    if bad < 5: print("MISMATCH", i, d, v, cnt, a, b)
print(f"fast_release 대조 {n}건 중 불일치 {bad}건")
assert bad == 0
