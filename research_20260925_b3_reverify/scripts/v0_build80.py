"""V0 - 정확한 80영업일 ctx 재구축 + 앵커 무결성. pandas3 단위버그 확인 필수."""
import sys, time, pickle
from pathlib import Path
from dataclasses import replace
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx80.pkl"
H._MEMO_PATH = HERE / "_memo80.pkl"
from app.trading.macd2 import config
from common import summarize

cd = ce._common_dates()
print("공통 날짜 %d개 · 최신 5: %s" % (len(cd), cd[-5:]), flush=True)
t0 = time.time()
ctx = H.build_ctx(80)
D = list(ctx.dates)
print("ctx %d일 %s~%s (%.0fs)" % (len(D), D[0], D[-1], time.time() - t0), flush=True)
print("9월 영업일: %s" % [d for d in D if d >= "20260901"], flush=True)

H.load_memo()
PROD = H._MEMO["base"]
Q3 = {}
pres = pickle.load(open(HERE / "_ctx76_preserved.pkl", "rb"))
W70 = pres["dates"][-70:]
print("W70 %s~%s 전부 ctx 안: %s" % (W70[0], W70[-1], all(d in set(D) for d in W70)), flush=True)

X = H.X2LITE
NB = {"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 4.0}
V = {"X2lite_W1": (None, {}, False, False), "H50": (None, {}, False, True),
     "N1": (dict(NB), {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}, True, True),
     "N1_Safe": (dict(NB), {"trail_stop_pct": 1.5}, True, True)}
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
        H._MEMO["base"] = Q3
        old = config.QUALITY_SCORE_THRESHOLD
        config.QUALITY_SCORE_THRESHOLD = 3
        try:
            tr = H.run_chain(ctx, p_, dates=W70, **kw)
        finally:
            config.QUALITY_SCORE_THRESHOLD = old
            H._MEMO["base"] = PROD
    else:
        tr = H.run_chain(ctx, p_, dates=W70, **kw)
    m = summarize(tr, W70)
    n_e, c_e = EXP[k]
    d = m["compound_pct"] - c_e
    good = m["trades"] == n_e and abs(d) < 0.01
    ok_all &= good
    print("앵커 %-11s n=%4d(기대 %d) 복리=%10.4f (기대 %9.4f, 차이 %+8.4f) %s %.0fs"
          % (k, m["trades"], n_e, m["compound_pct"], c_e, d, "OK" if good else "<<< 불일치",
             time.time() - t), flush=True)
H.save_memo()
print("\n80일 ctx 무결성:", "전부 일치" if ok_all else "불일치 — 재구축 ctx 사용 불가")
