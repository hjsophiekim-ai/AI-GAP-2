"""Z6 — BASE 재현성 격리 테스트 (20일 서브셋). READ-ONLY."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
from common import summarize
ctx = H.build_ctx(78)
D = ctx.dates[-20:]
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
print("axlib import 시 _q3base.pkl 로드됨?", (HERE/"_q3base.pkl").exists(), "len(Q3_BASE)=", len(A.Q3_BASE))

def fresh():
    for k in list(H._MEMO): H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}

def go(tag):
    ts = A.run("N1", ctx, D, ax=AX)
    m = summarize(ts, D)
    print("  %-22s n=%d 복리=%.4f PF=%.4f" % (tag, m["trades"], m["compound_pct"], m["pf"]), flush=True)
    return m

print("\n[A] 무패치 / 매번 완전 초기화")
fresh(); a1 = go("run1")
fresh(); a2 = go("run2 (동일해야 함)")
print("\n[B] 초기화 없이 연속 (memo 재사용)")
b1 = go("run3 (warm memo)")
print("\n[C] 스테일 _q3base.pkl 을 그대로 쓰면")
import pickle
for k in list(H._MEMO): H._MEMO[k] = {}
A.PROD_BASE = H._MEMO["base"]
A.Q3_BASE = pickle.load(open(HERE/"_q3base.pkl","rb")) if (HERE/"_q3base.pkl").exists() else {}
print("  len(stale Q3_BASE) =", len(A.Q3_BASE))
c1 = go("run4 (stale q3base)")
print("\n[D] zrelax + teg wrapper 설치, GATE=None / off")
import zrelax as Z
from app.trading.macd2 import teg_gate as TG
ORIG = TG.evaluate_teg
TG.evaluate_teg = lambda *a, **k: ORIG(*a, **k)
H.teg_gate.evaluate_teg = TG.evaluate_teg
Z.FEAT = Z.build_features(ctx.hynix_bars_3m); Z.install(); Z.set_gate(None)
fresh(); d1 = go("run5 (patched, off)")
