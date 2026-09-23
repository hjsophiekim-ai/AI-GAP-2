"""Y80 — X1 실적용 가능 모듈(A) 80영업일 전수 검증.

A = FLIP EXIT + AR1 + AFTERNOON BLUE BONUS
    * FLIP EXIT : run_chain(x1={"flip_exit": True}) — production 과 같은 자리
                  (기존 청산 전부의 뒤)
    * AR1       : zrelax 로 resolve_slot 의 SAME_DIRECTION 거절만 열고,
                  TEG 는 stack 면제만 적용(그 후보에 한정)
    * BLUE BONUS: 점수 가점 전용이라 단독 손익효과가 없다 -> 발생 건수만 집계

프로토콜: 단일 프로세스, 매 실행 전 _MEMO 전 버킷 + Q3_BASE 완전 초기화.
BASE 를 맨 앞/맨 뒤 두 번 돌려 재현성 확인(BASE == BASE2 아니면 중단).
"""
from __future__ import annotations

import gc
import pickle
import sys
import time
from dataclasses import replace as _rep
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache80"
H._CTX_CACHE = HERE / "_ctx80.pkl"
H._MEMO_PATH = HERE / "_memo80.pkl"
import zrelax as Z
from app.trading.macd2 import config, teg_gate as TG, x1_context as X1
from common import summarize

AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}

# ── AR1: TEG stack 면제를 **완화로 열린 후보에만** 적용 ────────────────────
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

ctx = H.build_ctx(80)
DATES = list(ctx.dates)
print("ctx %d일 %s~%s" % (len(DATES), DATES[0], DATES[-1]), flush=True)
assert len(DATES) == 80, "80일이 아니다: %d" % len(DATES)
Z.FEAT = Z.build_features(ctx.hynix_bars_3m)
Z.install()


def hard_reset():
    for k in list(H._MEMO):
        H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]
    A.Q3_BASE = {}
    gc.collect()


def run(tag, *, ar1: bool, flip_exit: bool):
    hard_reset()
    Z.set_gate((lambda f: True) if ar1 else None)
    Z.RELAXED_KEYS.clear()
    MODE["on"] = bool(ar1)
    x1 = {"flip_exit": True} if flip_exit else None
    t0 = time.time()
    ts = A.run("N1", ctx, DATES, ax=AX, x1=x1)
    log = list(H._LAST_X1_LOG)
    m = summarize(ts, DATES)
    print("%-6s 거래 %3d  복리 %9.4f  PF %.4f  MDD %7.3f  승률 %.1f%%  월 %.2f%%  (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], m["monthly_pct"], time.time() - t0), flush=True)
    return ts, m, log


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


print("\n[1] BASE (x1=None, AR1 off)")
base_ts, base_m, _ = run("BASE", ar1=False, flip_exit=False)
print("\n[2] X1-A (FLIP EXIT + AR1)")
x1_ts, x1_m, x1_log = run("X1-A", ar1=True, flip_exit=True)
print("\n[3] X1-A 분해: FLIP EXIT 단독")
fe_ts, fe_m, fe_log = run("FE만", ar1=False, flip_exit=True)
print("\n[4] X1-A 분해: AR1 단독")
ar_ts, ar_m, _ = run("AR1만", ar1=True, flip_exit=False)
print("\n[5] BASE 재실행 (재현성)")
base2_ts, base2_m, _ = run("BASE2", ar1=False, flip_exit=False)

same = sig(base_ts) == sig(base2_ts)
print("\nBASE 재현성:", "완전 일치" if same else "불일치!! -> 이후 수치 신뢰 불가")

pickle.dump({"dates": DATES, "base": base_ts, "x1": x1_ts, "fe": fe_ts, "ar": ar_ts,
             "bm": base_m, "xm": x1_m, "fem": fe_m, "arm": ar_m,
             "x1_log": x1_log, "fe_log": fe_log, "base_repro": same},
            open(HERE / "y80.pkl", "wb"))
print("saved y80.pkl")
