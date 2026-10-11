"""N1+C1 + SMART sizing 단일일 시뮬레이션. READ-ONLY.

usage: python y1_day.py YYYYMMDD
"""
import sys, math, pickle
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

HERE = Path(__file__).resolve().parent
TARGET = sys.argv[1] if len(sys.argv) > 1 else "20260922"

import axlib as A            # sets ce.CACHE_DIR / H._CTX_CACHE / H._MEMO_PATH
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"
H._MEMO_PATH = HERE / "_memo_922.pkl"
for _k in list(H._MEMO):
    H._MEMO[_k] = {}
A.PROD_BASE = H._MEMO["base"]
A.Q3_BASE = {}

from app.trading.macd2 import config, smart_sizing as ss
from app.trading.macd2.models import Direction
from common import summarize

ctx = H.build_ctx(78)
print("ctx %d일 %s~%s" % (len(ctx.dates), ctx.dates[0], ctx.dates[-1]), flush=True)
assert TARGET in ctx.dates, f"{TARGET} not in ctx"

ax = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ts = A.run("N1", ctx, [TARGET], ax=ax)
print("N1+C1 %s 거래 %d건" % (TARGET, len(ts)), flush=True)

# ── 1m frames for smart sizing inputs ────────────────────────────────
def load1m(tag):
    p = ce.CACHE_DIR / f"replay_{TARGET}_{tag}_1m.csv"
    d = pd.read_csv(p); d["datetime"] = pd.to_datetime(d["datetime"])
    if d["datetime"].dt.tz is None:
        d["datetime"] = d["datetime"].dt.tz_localize(config.KST)
    return d.sort_values("datetime").reset_index(drop=True)

etf1m = {config.LONG_SYMBOL: load1m("long"), config.INVERSE_SYMBOL: load1m("inverse")}
bars = ctx.hynix_bars_3m

rows = []
daily_notional = 0.0
DAILY_CAP = config.DEFAULT_BUDGET * config.X2LITE_SIZING_DAILY_EXPOSURE_CAP
for t in sorted(ts, key=lambda x: x["entry_time"] or ""):
    ent = pd.Timestamp(t["entry_time"]); ex = pd.Timestamp(t["exit_time"]) if t["exit_time"] else None
    direction = t["direction"]
    sym = t["entry_symbol"]
    # flag bar start = decision bar - 6min (flag bar -> confirm bar -> entry)
    dec = pd.Timestamp(bars["datetime"].iloc[t["decision_idx"]])
    flag_start = dec - timedelta(minutes=3)
    f = etf1m.get(sym)
    samples = [] if f is None else list(zip(f["datetime"], f["close"]))
    b3 = bars[bars["datetime"] <= dec]
    tox = ss.assess(bars_3m=b3, direction=direction, samples=samples,
                    confirm_start_at=flag_start.to_pydatetime(), entry_at=ent.to_pydatetime())
    slot = t["slot_number"]; sess = t["session"]
    if slot in (1, 2):
        p2 = config.P2_SIZING_SLOT12_MULT
    elif slot == 3 and sess == "MORNING":
        p2 = config.P2_SIZING_MORNING_SLOT3_MULT
    elif slot == 3:
        p2 = config.P2_SIZING_AFTERNOON_SLOT3_MULT
    else:
        p2 = 1.0
    mult = ss.smart_multiplier(p2, toxic=tox.toxic)
    w1a = float(t.get("w1a") or 1.0)
    want = config.DEFAULT_BUDGET * w1a * mult
    room = max(0.0, DAILY_CAP - daily_notional)
    notional = min(want, room)
    px = float(t["entry_price"] or 0)
    qty = int(notional // px) if px > 0 else 0
    daily_notional += qty * px
    net = float(t["net_pct"] or 0.0)
    rows.append(dict(
        slot=slot, session=sess, direction=direction, symbol=sym,
        flag_bar=flag_start.strftime("%H:%M"), confirm_bar=dec.strftime("%H:%M"),
        entry=ent.strftime("%H:%M"), entry_px=px,
        exit=ex.strftime("%m-%d %H:%M") if ex is not None else None,
        exit_px=t["exit_price"], exit_reason=t["exit_reason"],
        hold_min=t["hold_minutes"], net_pct=net, peak=t["peak_net_pct"], mae=t["mae_net_pct"],
        w1a=w1a, p2=p2, toxic=tox.toxic, tox_reason=tox.reason,
        conf_ret=tox.confirmation_return_pct, ema2050=tox.ema20_50_directional_pct,
        smart_mult=mult, qty=qty, notional=qty * px, pnl_krw=qty * px * net / 100.0,
        h50_held=t["h50_held"], h50_hold_at=t["h50_hold_at"], ax_mode=t["ax_mode"],
    ))
D = pd.DataFrame(rows)
D.to_csv(HERE / f"sim_{TARGET}.csv", index=False)
pd.set_option("display.width", 320)
print()
print(D.to_string(index=False))
print()
print("당일 투입원금 %s원 (일일한도 %s원) · SMART 총손익 %s원" % (
    format(D.notional.sum(), ",.0f"), format(DAILY_CAP, ",.0f"), format(D.pnl_krw.sum(), "+,.0f")))
base = (D.notional / D.smart_mult * D.net_pct / 100.0).sum()
print("참고: BASE 사이징(배수 1.0) 기준 총손익 %s원" % format(base, "+,.0f"))
