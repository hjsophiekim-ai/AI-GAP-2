"""Z8 — OFF parity: 래퍼를 설치해도 게이트 OFF 면 무패치와 완전히 같은가."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
from common import summarize
ctx = H.build_ctx(78); D = ctx.dates[-20:]
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
def fresh():
    for k in list(H._MEMO): H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
def go(tag):
    ts = A.run("N1", ctx, D, ax=AX); m = summarize(ts, D)
    sig = tuple(sorted((t["date"], t["entry_time"], round(t["net_pct"], 6)) for t in ts))
    print("  %-26s n=%d 복리=%.4f PF=%.4f" % (tag, m["trades"], m["compound_pct"], m["pf"]), flush=True)
    return sig
fresh(); s1 = go("무패치")
import zrelax as Z
from app.trading.macd2 import teg_gate as TG
ORIG = TG.evaluate_teg
MODE = {"m": "off"}
def wrap(bars, d, fb, da):
    r = ORIG(bars, d, fb, da)
    if r.approved or MODE["m"] == "off" or not r.conditions: return r
    return r
TG.evaluate_teg = wrap; H.teg_gate.evaluate_teg = wrap
Z.FEAT = Z.build_features(ctx.hynix_bars_3m); Z.install(); Z.set_gate(None)
fresh(); s2 = go("래퍼 설치 + 게이트 OFF")
print("\nOFF parity:", "완전 일치 (diff 0)" if s1 == s2 else "불일치!! diff=%d" % len(set(s1) ^ set(s2)))
