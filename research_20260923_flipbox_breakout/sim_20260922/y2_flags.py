"""9/22 플래그 전수 + 어떤 것이 진입으로 이어졌는지."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
ctx = H.build_ctx(78)
b = ctx.hynix_bars_3m
sim = pd.read_csv(HERE / "sim_20260922.csv")
entered = set(sim.confirm_bar)
print("2026-09-22 플래그 (엔진 ctx 기준)")
for i, d in sorted(ctx.flags_by_idx.items()):
    t = pd.Timestamp(b["datetime"].iloc[i])
    if t.strftime("%Y%m%d") != "20260922": continue
    conf = (t + pd.Timedelta(minutes=3)).strftime("%H:%M")
    mark = "  <== 진입" if conf in entered else ""
    print("  플래그봉 %s  확인봉 %s  %s  close=%s%s" % (
        t.strftime("%H:%M"), conf, d.value, format(int(b['close'].iloc[i]), ","), mark))
print()
last = b[b["datetime"].dt.strftime("%Y%m%d") == "20260922"]["datetime"].max()
print("ctx 내 9/22 마지막 3분봉:", last)
