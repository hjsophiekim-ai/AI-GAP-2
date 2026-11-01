"""85일 진단 — ① 승자 확대 여지 ② 손실 조기판별 가능성 (재생 불필요).

전부 **진입/청산 시점에 production 이 이미 알고 있는 값**만 쓴다. 사후 최고가를 보고
조건을 만들지 않는다 — 이 스크립트는 "분리가 되는가" 만 본다(임계값 탐색 없음).

지표는 production 함수를 그대로 호출한다:
  · HY 3분 MACD gap   `time_window_filter._gap_series`      (보유방향 부호)
  · EMA20/50 구조      `small_whipsaw_hold._structural_trend`
  · 세션 VWAP          `major_flag_filter._session_vwap`
  · 완성 3분봉         `signal_engine.resample_completed_3m`
MU·체결강도는 데이터가 10/15~ 뿐이라 85일 진단에서는 제외한다(제약).

    python .../h1_diag.py --tag L85_A --part win
    python .../h1_diag.py --tag L85_A --part loss
"""
import argparse
import json
import os
import statistics as st
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from a2_metrics import episodes  # noqa: E402
import a3_peaks as P  # noqa: E402
import r1_run  # noqa: E402
from app.trading.macd2 import config, major_flag_filter as MFF  # noqa: E402
from app.trading.macd2 import signal_engine as SE, small_whipsaw_hold as H50  # noqa: E402
from app.trading.macd2 import time_window_filter as TWF, worker as W  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
KST = config.KST
_BAR, _HY = {}, {}


def etf_bars(sym, day):
    tag = "long" if sym == config.LONG_SYMBOL else "inverse"
    k = (tag, day)
    if k not in _BAR:
        f = SP / "data" / f"replay_{day}_{tag}_1m.csv"
        if not f.exists():
            _BAR[k] = None
        else:
            x = pd.read_csv(f)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            _BAR[k] = x.set_index("datetime").sort_index()
    return _BAR[k]


def hy_bars(day, comp):
    if day not in _HY:
        i = comp.index(day)
        fr = []
        for d in (comp[i - 2], comp[i - 1], day):
            f = SP / "data" / f"replay_{d}_hynix_1m.csv"
            if f.exists():
                x = pd.read_csv(f)
                x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
                fr.append(x)
        h = pd.concat(fr).drop_duplicates("datetime", keep="last")
        h["datetime"] = h["datetime"].dt.tz_localize(KST)
        _HY[day] = h.sort_values("datetime").reset_index(drop=True)
    return _HY[day]


def mfe(sym, day, t0, minutes, avg, qty, high=True):
    """t0 이후 minutes 분의 최대 순수익률(ETF 1분봉 high). high=False 면 MAE(low)."""
    b = etf_bars(sym, day)
    if b is None:
        return None
    a = pd.Timestamp(t0).tz_localize(None)
    w = b[(b.index > a) & (b.index <= a + timedelta(minutes=minutes))]
    if not len(w):
        return None
    px = float(w["high"].max()) if high else float(w["low"].min())
    return W._net_return_pct(sym, avg, px, qty)


def ind(day, t, dirn, comp):
    """그 시점의 production 지표 — 완성 3분봉만 본다(미래 데이터 없음)."""
    hy = hy_bars(day, comp)
    df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
    bars = SE.resample_completed_3m(df1, now=t)
    work = MFF._prepare_bars(bars)
    sgn = 1.0 if dirn == Direction.UP_RED else -1.0
    out = {"gap_hold": None, "gap_expand": None, "ema": None, "vwap_ok": None}
    try:
        s = TWF._gap_series(work) if work is not None else None
        if s is not None and len(s) >= 2:
            g0 = float(s["gap"].iloc[-1]) * sgn
            g1 = float(s["gap"].iloc[-2]) * sgn
            out["gap_hold"] = bool(g0 > 0)
            out["gap_expand"] = bool(g0 > g1)
    except Exception:
        pass
    try:
        tr = H50._structural_trend(work)
        want = H50.TREND_UP if dirn == Direction.UP_RED else H50.TREND_DOWN
        out["ema"] = bool(tr == want)
    except Exception:
        pass
    try:
        if work is not None and len(work):
            vw = MFF._session_vwap(work)
            if len(vw) and pd.notna(vw.iloc[-1]) and float(vw.iloc[-1]) > 0:
                c = float(work["close"].iloc[-1])
                v = float(vw.iloc[-1])
                out["vwap_ok"] = bool(c >= v) if dirn == Direction.UP_RED else bool(c <= v)
    except Exception:
        pass
    return out


def collect(tag, days):
    rows = []
    for d in days:
        p = P.OUT / f"{tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        ent = {e["t"][11:19]: e for e in j.get("entries", [])}
        for e in episodes(p, d):
            if not e["entry_t"] or not e["exit_t"]:
                continue
            snap = ent.get(e["entry_t"][11:19], {})
            dirn = Direction.UP_RED if e["sym"] == config.LONG_SYMBOL else Direction.DOWN_BLUE
            rows.append(dict(ep=e, day=d, dirn=dirn, regime=snap.get("regime") or "?",
                             trend_ok=bool(snap.get("trend_ok"))))
    return rows


def part_win(rows, comp):
    print("\n" + "=" * 152)
    print("① 승자 확대 — TREND regime 거래의 청산 후 추가상승 전수")
    print("=" * 152)
    tr = [r for r in rows if r["regime"] == "TREND"]
    hdr = (f"{'날짜':8s}{'진입':9s}{'종목':8s}{'실현%':>7s}{'손익':>12s}"
           f"{'+3분':>7s}{'+6분':>7s}{'+9분':>7s}{'+15분':>7s}{'+30분':>7s}{'추가가능':>12s}"
           f"{'gap':5s}{'EMA':5s}{'VWAP':6s}  청산사유")
    print(hdr)
    agg = []
    for r in tr:
        e = r["ep"]
        sym, avg, qty = e["sym"], e["entry_px"], e["qty"]
        exit_net = W._net_return_pct(sym, avg, e["exit_px"], qty)
        ms = [mfe(sym, r["day"], e["exit_t"], m, avg, qty) for m in (3, 6, 9, 15, 30)]
        ok = [x for x in ms if x is not None]
        best = max(ok) if ok else None
        extra = ((best - exit_net) / 100.0 * avg * qty) if best is not None else None
        ix = ind(r["day"], pd.Timestamp(e["exit_t"]).to_pydatetime(), r["dirn"], comp)
        agg.append(dict(r=r, exit_net=exit_net, ms=ms, extra=extra, ix=ix))
        cells = "".join((f"{x:>7.2f}" if x is not None else f"{'-':>7s}") for x in ms)
        xs = f"{extra:>+12,.0f}" if extra is not None else f"{'-':>12s}"
        print(f"{r['day'][4:6]}/{r['day'][6:]:6s}{e['entry_t'][11:19]:9s}{sym:8s}{exit_net:>7.2f}"
              f"{e['pnl']:>+12,.0f}{cells}{xs}"
              f"{('O' if ix['gap_hold'] else '.'):5s}{('O' if ix['ema'] else '.'):5s}"
              f"{('O' if ix['vwap_ok'] else '.'):6s}  {';'.join(e['exits'])[:34]}")
    up = [a for a in agg if a["extra"] is not None and a["extra"] > 0]
    print(f"\nTREND 거래 {len(tr)}건 · 청산 후 더 간 거래 {len(up)}건 · "
          f"추가가능 합 {sum(a['extra'] for a in up):+,.0f}원")
    print("  (**상한**이다 — 사후 최고가에 판다는 가정. 실제 연장은 그보다 낮게 끝난다.)")
    print("\n[청산시점 지표 × 추가상승 분리도]")
    for key, lab in (("gap_hold", "HY gap 보유방향 유지"), ("gap_expand", "HY gap 재확대"),
                     ("ema", "EMA20/50 추세 유지"), ("vwap_ok", "VWAP 우위 유지")):
        for val in (True, False):
            s = [a for a in agg if a["ix"][key] is val]
            if not s:
                continue
            ex = [a["extra"] for a in s if a["extra"] is not None]
            pos = [x for x in ex if x > 0]
            med = st.median(ex) if ex else 0.0
            print(f"  {lab:22s} {str(val):5s} 거래 {len(s):>3d} · 추가상승 {len(pos):>3d}건 "
                  f"({(len(pos) / len(s) * 100) if s else 0:>5.1f}%) · 합 {sum(pos):>+12,.0f} · "
                  f"중앙 {med:>+10,.0f}")


def part_loss(rows, comp):
    print("\n" + "=" * 152)
    print("② 손실 조기판별 — 진입 후 3/6/9분 시점 비교")
    print("=" * 152)
    WI, LO = [], []
    for r in rows:
        e = r["ep"]
        sym, avg, qty = e["sym"], e["entry_px"], e["qty"]
        rec = {"r": r, "win": e["pnl"] > 0}
        for m in (3, 6, 9):
            rec[f"mfe{m}"] = mfe(sym, r["day"], e["entry_t"], m, avg, qty, True)
            rec[f"mae{m}"] = mfe(sym, r["day"], e["entry_t"], m, avg, qty, False)
        t6 = pd.Timestamp(e["entry_t"]).to_pydatetime() + timedelta(minutes=6)
        rec["ix6"] = ind(r["day"], t6, r["dirn"], comp)
        (WI if rec["win"] else LO).append(rec)
    print(f"  승 {len(WI)}건 / 패 {len(LO)}건\n")
    print(f"  {'지표':26s}{'승 중앙':>12s}{'승 25%':>10s}{'패 중앙':>12s}{'패 75%':>10s}  분리")

    def q(a, p):
        return a[max(0, min(len(a) - 1, int(len(a) * p)))]

    for m in (3, 6, 9):
        for k, lab in ((f"mfe{m}", f"진입후 {m}분 MFE%"), (f"mae{m}", f"진입후 {m}분 MAE%")):
            wv = sorted(x[k] for x in WI if x[k] is not None)
            lv = sorted(x[k] for x in LO if x[k] is not None)
            if not wv or not lv:
                continue
            sep = "***" if q(wv, 0.25) > q(lv, 0.75) else ""
            print(f"  {lab:26s}{st.median(wv):>12.3f}{q(wv, 0.25):>10.3f}"
                  f"{st.median(lv):>12.3f}{q(lv, 0.75):>10.3f}  {sep}")
    print("\n  [진입 후 6분 시점 production 지표]")
    for key, lab in (("gap_hold", "HY gap 보유방향 유지"), ("gap_expand", "HY gap 재확대"),
                     ("ema", "EMA20/50 추세 유지"), ("vwap_ok", "VWAP 우위 유지")):
        wt = sum(1 for x in WI if x["ix6"][key] is True)
        wn = sum(1 for x in WI if x["ix6"][key] is not None)
        lt = sum(1 for x in LO if x["ix6"][key] is True)
        ln = sum(1 for x in LO if x["ix6"][key] is not None)
        print(f"    {lab:22s} 승 {wt}/{wn} ({(wt / wn * 100) if wn else 0:>5.1f}%)  ·  "
              f"패 {lt}/{ln} ({(lt / ln * 100) if ln else 0:>5.1f}%)")
    print("\n  [6분까지 진입방향으로 한 번도 확장 못한 거래]  (MFE6 <= 0)")
    for lab, S in (("승", WI), ("패", LO)):
        n = sum(1 for x in S if x["mfe6"] is not None and x["mfe6"] <= 0)
        d = sum(1 for x in S if x["mfe6"] is not None)
        print(f"    {lab} {n}/{d} ({(n / d * 100) if d else 0:.1f}%)")
    print("\n  [조합: 6분 MFE<=0 AND gap 보유방향 상실]")
    for lab, S in (("승", WI), ("패", LO)):
        n = sum(1 for x in S if x["mfe6"] is not None and x["mfe6"] <= 0
                and x["ix6"]["gap_hold"] is False)
        print(f"    {lab} {n}/{len(S)}건")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="L85_A")
    ap.add_argument("--part", default="win", choices=("win", "loss", "both"))
    a = ap.parse_args()
    comp = r1_run.complete_days()
    days = comp[-85:]
    rows = collect(a.tag, days)
    print(f"[{a.tag}] 85일 {len(rows)}거래 로드")
    if a.part in ("win", "both"):
        part_win(rows, comp)
    if a.part in ("loss", "both"):
        part_loss(rows, comp)


if __name__ == "__main__":
    main()
