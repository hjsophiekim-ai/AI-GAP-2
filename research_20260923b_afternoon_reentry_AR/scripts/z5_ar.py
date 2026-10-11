"""Z5 — AR (Afternoon Re-entry) 규칙 개발/검증. READ-ONLY, production 무수정.

AR0 : 오후 동일방향 차단만 해제 (TEG 그대로)
AR1 : AR0 + "TEG 단일 탈락이 price_ema_stack_aligned 뿐이고 모멘텀 2종 + VWAP 이
      전부 참이면 stack 을 면제" — **완화로 열린 후보에만** 적용(범위 한정)
PLB : AR1 의 stack 면제를 오후 TEG 후보 '전체'에 적용 (blast radius 대조군)
"""
import sys, pickle, time; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
for _k in list(H._MEMO): H._MEMO[_k] = {}
A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
import zrelax as Z
from app.trading.macd2 import teg_gate as TG, config
from common import summarize

ORIG_TEG = TG.evaluate_teg
MODE = {"stack_exempt": "off"}     # off | scoped | all


def teg_wrapper(bars_3m, flag_direction, flag_bar_dt, decision_at):
    d = ORIG_TEG(bars_3m, flag_direction, flag_bar_dt, decision_at)
    if d.approved or MODE["stack_exempt"] == "off" or not d.conditions:
        return d
    kst = decision_at.astimezone(config.KST)
    key = (kst.strftime("%Y%m%d"), kst.strftime("%H:%M"))
    if MODE["stack_exempt"] == "scoped" and key not in Z.RELAXED_KEYS:
        return d
    failing = [c for c in TG.ALL_CONDITIONS if not d.conditions.get(c, False)]
    if failing != [TG.COND_EMA_STACK]:
        return d
    if not (d.conditions.get(TG.COND_MACD_GAP_EXPANDING) and
            d.conditions.get(TG.COND_EMA_SPREAD_EXPANDING) and
            d.conditions.get(TG.COND_VWAP)):
        return d
    from dataclasses import replace as _rep
    return _rep(d, approved=True, reject_reasons=tuple(list(d.reject_reasons) + ["STACK_EXEMPT"]))


TG.evaluate_teg = teg_wrapper
import hengine5 as _h
_h.teg_gate.evaluate_teg = teg_wrapper

ctx = H.build_ctx(78)
Z.FEAT = Z.build_features(ctx.hynix_bars_3m); Z.install()
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}


def run(tag, gate, stack_mode):
    Z.set_gate(gate); Z.RELAXED_KEYS.clear(); MODE["stack_exempt"] = stack_mode
    for _k in list(H._MEMO): H._MEMO[_k] = {}   # 변형 간 memo 오염 차단 (전 버킷 초기화)
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
    t0 = time.time(); ts = A.run("N1", ctx, ctx.dates, ax=AX)
    m = summarize(ts, ctx.dates)
    print("%-6s n=%3d 복리=%9.4f PF=%.4f MDD=%.3f 월%.2f%% 승률%.1f%% top5제외=%.1f (%.0fs)" % (
        tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["monthly_pct"],
        m["win_rate_pct"], m["top5_excl_pct"], time.time() - t0), flush=True)
    return ts, m


out = {}
out["BASE"] = run("BASE", None, "off")
out["AR0"] = run("AR0", lambda f: True, "off")
out["AR1"] = run("AR1", lambda f: True, "scoped")
out["PLB"] = run("PLB", lambda f: True, "all")
out["BASE2"] = run("BASE2", None, "off")   # 프로토콜 안정성 확인 (BASE 와 같아야 함)
pickle.dump({k: (v[1]) for k, v in out.items()}, open(HERE / "z5_summary.pkl", "wb"))
pickle.dump({k: v[0] for k, v in out.items()}, open(HERE / "z5_trades.pkl", "wb"))

def key(t): return (t["date"], t["entry_time"], t["direction"])
b = {key(t): t for t in out["BASE"][0]}
for nm in ("AR0", "AR1", "PLB"):
    v = {key(t): t for t in out[nm][0]}
    add = [v[k] for k in v if k not in b]; rem = [b[k] for k in b if k not in v]
    print("\n--- %s : +%d / -%d ---" % (nm, len(add), len(rem)))
    if add:
        D = pd.DataFrame([dict(date=t["date"], entry=pd.Timestamp(t["entry_time"]).strftime("%H:%M"),
                               dir=t["direction"][:1], slot=t["slot_number"],
                               exit=pd.Timestamp(t["exit_time"]).strftime("%H:%M"),
                               reason=t["exit_reason"], net=t["net_pct"], peak=t["peak_net_pct"]) for t in add])
        pd.set_option("display.width", 220)
        print(D.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
        print("   net합 %+.3f%%p  승%d/패%d" % (D.net.sum(), (D.net > 0).sum(), (D.net <= 0).sum()))
    if rem:
        print("   사라짐:", [(t["date"], pd.Timestamp(t["entry_time"]).strftime("%H:%M"), round(t["net_pct"],3)) for t in rem])
