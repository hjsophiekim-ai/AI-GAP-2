"""Y82 — B) X1-1 MORNING WATCH / X1-4 LATE ENTRY shadow 연구 (80일).

**손익을 합산하지 않는다.** production 에 아직 진입경로가 없으므로
"이런 후보가 몇 번, 어떤 조건에서 생겼고 그 뒤 가격이 어땠나"만 본다.
"""
from __future__ import annotations

import pickle
import sys
from datetime import time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache80"
H._CTX_CACHE = HERE / "_ctx80.pkl"
H._MEMO_PATH = HERE / "_memo80.pkl"
from app.trading.macd2 import config, x1_context as X
from app.trading.macd2.models import Direction

KST = config.KST
Z = pickle.load(open(HERE / "y80.pkl", "rb"))
DATES = Z["dates"]
ctx = H.build_ctx(80)
bars = ctx.hynix_bars_3m
BAR = {pd.Timestamp(t): i for i, t in enumerate(bars["datetime"])}
HOR = [3, 6, 9, 15, 30, 60]


def load1m(d, tag):
    p = ce.CACHE_DIR / f"replay_{d}_{tag}_1m.csv"
    if not p.exists():
        return None
    x = pd.read_csv(p)
    x["datetime"] = pd.to_datetime(x.datetime)
    if x.datetime.dt.tz is None:
        x["datetime"] = x.datetime.dt.tz_localize(KST)
    return x.sort_values("datetime").reset_index(drop=True)


ONE = {}
FLAGS = {}
for i, d in sorted(ctx.flags_by_idx.items()):
    ts = pd.Timestamp(bars["datetime"].iloc[i])
    ds = ts.strftime("%Y%m%d")
    if ds in set(DATES) and "09:00" <= ts.strftime("%H:%M") <= "15:20":
        FLAGS.setdefault(ds, []).append({"at": ts, "direction": d.value,
                                         "source": X.LIVE_CONFIRMED})

BASE_ENTRIES = {(t["date"], pd.Timestamp(t["entry_time"]).strftime("%H:%M"))
                for t in Z["base"]}


def px(df, t):
    if df is None:
        return None
    s = df[df.datetime <= t]
    return float(s.close.iloc[-1]) if len(s) else None


def fwd(df, t0, sign):
    """방향정규화 forward 수익률 + MFE/MAE (60분)."""
    if df is None:
        return {}, None, None
    e = px(df, t0)
    if not e:
        return {}, None, None
    out = {}
    for h in HOR:
        v = px(df, t0 + timedelta(minutes=h))
        last = df.datetime.iloc[-1]
        out[h] = (None if (v is None or t0 + timedelta(minutes=h) > last)
                  else sign * (v - e) / e * 100)
    seg = df[(df.datetime >= t0) & (df.datetime <= t0 + timedelta(minutes=60))]
    if not len(seg):
        return out, None, None
    hi, lo = float(seg.high.max()), float(seg.low.min())
    mfe = sign * (hi - e) / e * 100 if sign > 0 else sign * (lo - e) / e * 100
    mae = sign * (lo - e) / e * 100 if sign > 0 else sign * (hi - e) / e * 100
    return out, mfe, mae


watch_rows, late_rows = [], []
for day in DATES:
    ev = FLAGS.get(day, [])
    if not ev:
        continue
    ONE.setdefault(day, {t: load1m(day, t) for t in ("hynix", "long", "inverse")})
    hx = ONE[day]["hynix"]
    for f in ev:
        at = f["at"]
        d = Direction.UP_RED if f["direction"] == Direction.UP_RED.value else Direction.DOWN_BLUE
        dec = at + pd.Timedelta(minutes=6)           # T+3 판정 시각
        if dec.time() > dtime(15, 20):
            continue
        sl = bars[bars["datetime"] <= dec]
        tag = "long" if d == Direction.UP_RED else "inverse"
        sign = 1.0
        etf = ONE[day][tag]
        base_taken = (day, (dec + pd.Timedelta(minutes=3)).strftime("%H:%M")) in BASE_ENTRIES \
            or (day, dec.strftime("%H:%M")) in BASE_ENTRIES

        # ── X1-1 MORNING WATCH ────────────────────────────────────────────
        if dec.time() < config.TW2_3SLOT_MORNING_WINDOW_END:
            m = X.evaluate_morning_context(sl, hx, d, now=dec.to_pydatetime())
            if m.action == X.ACTION_WATCH:
                rel_at = rel_ok = None
                for k in (1, 2, 3):
                    t2 = dec + pd.Timedelta(minutes=3 * k)
                    ok, why, _ = X.evaluate_morning_watch_release(
                        bars[bars["datetime"] <= t2], d, now=t2.to_pydatetime(),
                        watch_started_at=dec.to_pydatetime())
                    if ok:
                        rel_at, rel_ok = t2, True
                        break
                t0 = (rel_at or dec) + pd.Timedelta(minutes=3)
                r, mfe, mae = fwd(etf, t0, sign)
                watch_rows.append(dict(
                    date=day, flag=at.strftime("%H:%M"), dec=dec.strftime("%H:%M"),
                    dir="R" if d == Direction.UP_RED else "B", score=m.score,
                    premarket=m.premarket_trend or "", base_taken=base_taken,
                    released=bool(rel_ok),
                    rel_at=(rel_at.strftime("%H:%M") if rel_at is not None else ""),
                    price=px(etf, t0), **{f"+{h}m": r.get(h) for h in HOR},
                    MFE=mfe, MAE=mae))

        # ── X1-4 LATE ENTRY ───────────────────────────────────────────────
        w = X.evaluate_flip_watch(ev, now=dec.to_pydatetime(), bars_3m=sl)
        if not w.watching:
            continue
        for k in (1, 2, 3, 4, 5):
            t2 = dec + pd.Timedelta(minutes=3 * k)
            if t2.time() > dtime(15, 20):
                break
            le = X.evaluate_late_entry(
                bars[bars["datetime"] <= t2], w, now=t2.to_pydatetime(),
                base_reject_reason="REJECT_LOW_QUALITY_SCORE")
            if le.candidate:
                ld = X._as_direction(w.last_direction)
                ltag = "long" if ld == Direction.UP_RED else "inverse"
                letf = ONE[day][ltag]
                t0 = t2 + pd.Timedelta(minutes=3)
                r, mfe, mae = fwd(letf, t0, 1.0)
                late_rows.append(dict(
                    date=day, cluster=w.seq, flips=w.flip_count, flags=w.flag_count,
                    span=w.span_min, dir="R" if ld == Direction.UP_RED else "B",
                    breakout=t2.strftime("%H:%M"), score=le.score,
                    lat=le.latency_min, bucket=le.latency_bucket,
                    guard=le.within_guard, base_taken=base_taken,
                    price=px(letf, t0), **{f"+{h}m": r.get(h) for h in HOR},
                    MFE=mfe, MAE=mae))
                break

W = pd.DataFrame(watch_rows)
L = pd.DataFrame(late_rows).drop_duplicates(subset=["date", "breakout", "dir"]) \
    if late_rows else pd.DataFrame()
W.to_csv(HERE / "y82_watch.csv", index=False)
L.to_csv(HERE / "y82_late.csv", index=False)
pd.set_option("display.width", 300)


def stats(df, name):
    print("\n" + "=" * 118)
    print("【%s】 %d건 / %d일" % (name, len(df), df.date.nunique() if len(df) else 0))
    print("=" * 118)
    if not len(df):
        return
    print(df.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print("\n  방향정규화 사후수익 (손익 합산 아님, 참고용)")
    for h in HOR:
        v = df[f"+{h}m"].dropna()
        if len(v):
            print("    +%2dm  n=%3d  승률 %5.1f%%  평균 %+.3f%%  중앙 %+.3f%%"
                  % (h, len(v), (v > 0).mean() * 100, v.mean(), v.median()))
    mf, ma = df.MFE.dropna(), df.MAE.dropna()
    if len(mf):
        print("    MFE60 평균 %+.2f%% / MAE60 평균 %+.2f%%" % (mf.mean(), ma.mean()))
    print("    BASE 가 이미 잡은 신호: %d / %d" % (int(df.base_taken.sum()), len(df)))


stats(W, "B-1. X1-1 MORNING WATCH 후보 (오전 역행 의심 플래그)")
if len(W):
    print("    9분 내 재승인(release): %d / %d" % (int(W.released.sum()), len(W)))
stats(L, "B-2. X1-4 LATE ENTRY 후보 (flip cluster box breakout)")
if len(L):
    print("    15분 guard 통과: %d / %d" % (int(L.guard.sum()), len(L)))
print("\nsaved y82_watch.csv / y82_late.csv")
