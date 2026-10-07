"""A0 — 엔진 사본 무결성 확인. 공표 앵커 4종(W70) 재현.

READ-ONLY. production 무수정. scratchpad 사본에서만 실행.
"""
import sys, time, pickle
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from dataclasses import replace

HERE = Path(__file__).resolve().parent

import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache"
H._CTX_CACHE = HERE / "_ctx_B.pkl"
H._MEMO_PATH = HERE / "_memo_B.pkl"

from app.trading.macd2 import config
from common import summarize

H.load_memo()
PROD_BASE = H._MEMO["base"]
Q3_BASE = {}

t0 = time.time()
ctx = H.build_ctx(78)
print(f"ctx {len(ctx.dates)}일 {ctx.dates[0]}~{ctx.dates[-1]} ({time.time()-t0:.0f}s)", flush=True)

pres = pickle.load(open(HERE / "_ctx76_preserved.pkl", "rb"))
W70 = pres["dates"][-70:]
print(f"W70 {W70[0]}~{W70[-1]}  전부 ctx 안: {all(d in set(ctx.dates) for d in W70)}", flush=True)

X = H.X2LITE
NB = {"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 4.0}
V = {
    "X2lite_W1": (None,     {},                                        False, False),
    "H50":       (None,     {},                                        False, True),
    "N1":        (dict(NB), {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}, True, True),
    "N1_Safe":   (dict(NB), {"trail_stop_pct": 1.5},                    True, True),
}
EXP = {"X2lite_W1": (144, 176.3247), "H50": (141, 204.1759),
       "N1": (142, 396.6787), "N1_Safe": (142, 384.48)}

ok_all = True
for k, (cfg, po, q3, h50) in V.items():
    t = time.time()
    if cfg is not None:
        H.D_VARIANTS[k] = cfg
    p_ = replace(X, **po) if po else X
    kw = dict(h50=h50)
    if cfg is not None:
        kw["d_variant"] = k
    if q3:
        H._MEMO["base"] = Q3_BASE
        old = config.QUALITY_SCORE_THRESHOLD
        config.QUALITY_SCORE_THRESHOLD = 3
        try:
            tr = H.run_chain(ctx, p_, dates=W70, **kw)
        finally:
            config.QUALITY_SCORE_THRESHOLD = old
            H._MEMO["base"] = PROD_BASE
    else:
        tr = H.run_chain(ctx, p_, dates=W70, **kw)
    m = summarize(tr, W70)
    n_e, c_e = EXP[k]
    d = m["compound_pct"] - c_e
    ok = m["trades"] == n_e and abs(d) < 0.01
    ok_all &= ok
    print(f"앵커 {k:12s} n={m['trades']:4d}(기대 {n_e}) 복리={m['compound_pct']:10.4f} "
          f"(기대 {c_e:9.4f}, 차이 {d:+8.4f}) {'OK' if ok else '<<< 불일치'} {time.time()-t:.0f}s",
          flush=True)
H.save_memo()
print("\n무결성:", "전부 일치" if ok_all else "불일치 — 이후 결과 신뢰 불가")
