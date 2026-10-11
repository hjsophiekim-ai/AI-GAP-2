"""rlib — 9월형 regime 방어 연구 공용 셋업. READ-ONLY (production 무수정).

BASE = N1 + C1(ax pp arm5.0/give1.5 gap_neg) + AR1(zrelax + TEG stack 면제).
FLIP EXIT 은 전 실험에서 OFF (엔진 사본에 x1 경로 자체가 없다).
_q3base.pkl 은 비활성화(.DISABLED) — Q3_BASE 는 매 런 새로 계산한다.
"""
import gc, pickle, sys, time
from pathlib import Path
from dataclasses import replace as _rep
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import axlib as A, hengine5 as H
import zrelax as Z
from app.trading.macd2 import config, teg_gate as TG
from common import summarize

AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}

# ── AR1: 완화로 열린 후보에만 TEG stack 면제 (y80_run.py 와 동일) ──────────
_ORIG_TEG = TG.evaluate_teg
MODE = {"on": False}


def teg_wrapper(bars_3m, flag_direction, flag_bar_dt, decision_at):
    d = _ORIG_TEG(bars_3m, flag_direction, flag_bar_dt, decision_at)
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
                reject_reasons=tuple(list(d.reject_reasons) + ["AR1_STACK_EXEMPT"]))


TG.evaluate_teg = teg_wrapper
H.teg_gate.evaluate_teg = teg_wrapper


def get_ctx():
    c = H.build_ctx(78)
    Z.FEAT = Z.build_features(c.hynix_bars_3m)
    Z.install()
    return c


def hard_reset():
    """memo 전 버킷 + Q3_BASE 완전 초기화 (80일 연구 프로토콜과 동일)."""
    for k in list(H._MEMO):
        H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]
    A.Q3_BASE = {}
    gc.collect()


def run(ctx, dates, *, rx=None, tag=""):
    hard_reset()
    Z.set_gate(lambda f: True)
    Z.RELAXED_KEYS.clear()
    MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, dates, ax=AX, **({"rx": rx} if rx else {}))
    m = summarize(ts, dates)
    if tag:
        print("%-22s 거래 %3d  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
              % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"],
                 m["win_rate_pct"], time.time() - t0), flush=True)
    return ts, m


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)
