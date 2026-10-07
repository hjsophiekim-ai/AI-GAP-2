"""b3lib — B3 TP 민감도 연구 공용 셋업. READ-ONLY (production 무수정).

BASE = N1 + C1 + SMART(w1a) + AR1  (research_20260923c_x1_80d y80 의 "AR1만" 과 동일 경로)
proj = git archive 948c211 (X1 머지 커밋; y80 당시 트리)
"""
from __future__ import annotations

import gc
import pickle
import sys
from dataclasses import replace as _rep
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import os
NDAYS = int(os.environ.get("B3_NDAYS", "80"))
ce.CACHE_DIR = HERE / ("cache80" if NDAYS == 80 else f"cache{NDAYS}")
H._CTX_CACHE = HERE / f"_ctx{NDAYS}.pkl"
H._MEMO_PATH = HERE / "_memo80_unused.pkl"
import zrelax as Z  # noqa: E402
from app.trading.macd2 import config, teg_gate as TG  # noqa: E402

AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}

ORIG_TEG = TG.evaluate_teg
MODE = {"on": False}


def teg_wrapper(bars_3m, flag_direction, flag_bar_dt, decision_at):
    d = ORIG_TEG(bars_3m, flag_direction, flag_bar_dt, decision_at)
    if d.approved or not MODE["on"] or not d.conditions:
        return d
    kst = decision_at.astimezone(config.KST)
    if (kst.strftime("%Y%m%d"), kst.strftime("%H:%M")) not in Z.RELAXED_KEYS:
        return d
    failing = [c for c in TG.ALL_CONDITIONS if not d.conditions.get(c, False)]
    if failing != [TG.COND_EMA_STACK]:
        return d
    if not (d.conditions.get(TG.COND_MACD_GAP_EXPANDING)
            and d.conditions.get(TG.COND_EMA_SPREAD_EXPANDING)
            and d.conditions.get(TG.COND_VWAP)):
        return d
    return _rep(d, approved=True,
                reject_reasons=tuple(list(d.reject_reasons) + ["X1_AR1_STACK_EXEMPT"]))


TG.evaluate_teg = teg_wrapper
H.teg_gate.evaluate_teg = teg_wrapper

ctx = H.build_ctx(NDAYS)
DATES = list(ctx.dates)
assert len(DATES) == NDAYS and DATES[0] == "20260527", (len(DATES), DATES[0], DATES[-1])
print("ctx", len(DATES), DATES[0], DATES[-1], flush=True)
Z.FEAT = Z.build_features(ctx.hynix_bars_3m)
Z.install()

# Y3 용 MACD hist — production p3_stack.macd_hist_series 와 같은 식
_c = ctx.hynix_bars_3m["close"].astype(float)
_m = (_c.ewm(span=int(config.P3_Y3_MACD_FAST) if hasattr(config, "P3_Y3_MACD_FAST") else 12, adjust=False).mean()
      - _c.ewm(span=26, adjust=False).mean())
HIST = (_m - _m.ewm(span=9, adjust=False).mean()).to_numpy()
EMA20 = _c.ewm(span=20, adjust=False).mean().to_numpy()
EMA50 = _c.ewm(span=50, adjust=False).mean().to_numpy()


def hard_reset():
    for k in list(H._MEMO):
        H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]
    A.Q3_BASE = {}
    gc.collect()


def make_regime(base_trades, strict=True):
    """SHADOW-BASE SLOW detector. 판정시각보다 먼저 끝난 BASE 거래 10건."""
    rows = sorted(((pd.Timestamp(t["exit_time"]), bool(t["h50_held"]), bool(t["tp1_hit"]))
                   for t in base_trades), key=lambda r: r[0])
    ex = [r[0] for r in rows]

    def fn(as_of):
        a = pd.Timestamp(as_of)
        import bisect
        k = bisect.bisect_left(ex, a) if strict else bisect.bisect_right(ex, a)
        if k < 10:
            return "WARMUP"
        rec = rows[k - 10:k]
        h = sum(r[1] for r in rec) / 10.0
        tp = sum(r[2] for r in rec) / 10.0
        return "CHOP" if (h >= 0.40 and tp <= 0.20) else "TREND"
    return fn


def run(b3=None):
    hard_reset()
    Z.set_gate(lambda f: True)
    Z.RELAXED_KEYS.clear()
    MODE["on"] = True
    if b3 is not None:
        b3 = dict(b3)
        b3["hist"] = HIST
        b3["ema20"] = EMA20
        b3["ema50"] = EMA50
    return A.run("N1", ctx, DATES, ax=AX, b3=b3)


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)
