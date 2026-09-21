"""ctx(78) 빌드. READ-ONLY (data/cache 읽기만)."""
import sys, time
sys.stdout.reconfigure(encoding="utf-8")
t0 = time.time()
import axlib as A
import hengine5 as H
print(f"CACHE_DIR = {A.ce.CACHE_DIR}", flush=True)
D = A.ce._common_dates()
print(f"common dates = {len(D)}  {D[0]}..{D[-1]}", flush=True)
c = A.ctx(78)
print(f"ctx built in {time.time()-t0:.0f}s  dates={len(c.dates)} "
      f"{c.dates[0]}..{c.dates[-1]}  flags={len(c.flags_by_idx)}", flush=True)
print(f"cached -> {H._CTX_CACHE}  exists={H._CTX_CACHE.exists()}", flush=True)
