"""한 거래의 **추세약화 신호 6종 타임라인**을 20초 격자로 오프라인 재구성한다.

하네스가 보유 중 매 tick 에 계산하는 것과 **같은 입력·같은 production 함수**를 쓴다.
하네스는 활성화 시점 하나만 기록하므로, "신호 2개가 일시적으로만 겹친 것인지"를 보려면
이 전체 타임라인이 필요하다.

    python .../a7_trace.py --day 20261029 --entry 09:59:20 --tag CHOPX_A

출력: 시각 / 틱최고% / 현재% / 최저bid% / 신호 6종 / W1·W2 활성여부 / 보호선 / 이탈여부
"""
import argparse
import json
import os
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
from app.trading.macd2 import config, exec_strength as XS, major_flag_filter as MFF  # noqa: E402
from app.trading.macd2 import mu_assist as MU, signal_engine as SE  # noqa: E402
from app.trading.macd2 import time_window_filter as TWF, worker as W  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))
KST = config.KST
LADDER = ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5))
SIGS = ["s1_gap_shrink", "s2_mu_weak", "s3_cttr_weak", "s4_giveback", "s5_vwap_lost", "s6_opp_pending"]


def line_for(peak):
    for lv, ln in LADDER:
        if peak >= lv:
            return ln
    return None


def hy_frame(days):
    fr = []
    for d in days:
        p = SP / "data" / f"replay_{d}_hynix_1m.csv"
        if p.exists():
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            fr.append(x)
    hy = pd.concat(fr).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    return hy.sort_values("datetime").reset_index(drop=True)


def cttr_series(day):
    p = RT / "snapshots" / day / "000660.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["ts", "cttr"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df.dropna(subset=["cttr"]).set_index("ts")["cttr"].sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--entry", required=True, help="HH:MM:SS")
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--prev", nargs=2, default=None, help="전전일 전일 (기본 자동)")
    a = ap.parse_args()
    d = a.day
    day = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    src = P.OUT / f"{a.tag}_{d}.json"
    j = json.loads(src.read_text(encoding="utf-8"))
    eps = [e for e in episodes(src, d) if (e["entry_t"] or "")[11:19] == a.entry]
    if not eps:
        print("그 진입시각의 거래가 없다:", [e["entry_t"] for e in episodes(src, d)])
        return
    e = eps[0]
    sym, avg, qty = e["sym"], float(e["entry_px"]), int(e["qty"])
    dirn = Direction.UP_RED if sym == config.LONG_SYMBOL else Direction.DOWN_BLUE
    sgn = 1.0 if dirn == Direction.UP_RED else -1.0
    t_in = pd.Timestamp(e["entry_t"]).to_pydatetime()
    t_out = pd.Timestamp(e["exit_t"]).to_pydatetime()

    comp = sorted({f.stem.split("_")[1] for f in (SP / "data").glob("replay_*_hynix_1m.csv")})
    i = comp.index(d)
    days = a.prev or [comp[i - 2], comp[i - 1]]
    hy = hy_frame(list(days) + [d])
    ctt = cttr_series(day)
    tk = P.ticks(sym, day)

    # 반대플래그 T+3 미확정 구간 (그 재생의 판정기록에서 복원)
    opp_dir = "DOWN_BLUE" if dirn == Direction.UP_RED else "UP_RED"
    pend = []
    cur = {}
    for g in j.get("gates", []):
        if g["ev"] != "DECISION":
            continue
        base = str(g.get("sid") or "").split(":")[0]
        if not base:
            continue
        if g.get("block") == config.TW_PENDING_CONFIRMATION:
            cur[base] = g["t"]
        elif base in cur:
            pend.append((cur.pop(base), g["t"], "UP_RED" if base.endswith("UP_RED") else "DOWN_BLUE"))
    for b, t0 in cur.items():
        pend.append((t0, "9999", "UP_RED" if b.endswith("UP_RED") else "DOWN_BLUE"))

    print(f"\n{'='*140}")
    print(f"[{a.tag}] {day} {a.entry} {sym}  진입 {avg:,.0f} x{qty}  실제청산 {e['exit_t'][11:19]} "
          f"{e['exit_px']:,.0f} ({e['ret_pct']:+.2f}%, {e['pnl']:+,.0f}원)  사유 {'→'.join(e['exits'])}")
    print(f"{'='*140}")
    print(f"{'시각':10s}{'틱최고%':>8s}{'현재%':>7s}{'최저bid%':>9s}  "
          + "".join(f"{s.split('_')[0]:>4s}" for s in SIGS)
          + f"{'n':>3s}{'W1선':>6s}{'W2선':>6s}  비고")

    peak = 0.0
    mu_confirm = False
    st = {"W1": {"act": False, "line": None, "out": None},
          "W2": {"act": False, "line": None, "out": None}}
    bars = mu_pat = None
    cur_min = None
    t = t_in
    while t <= t_out:
        nv = pd.Timestamp(t).tz_localize(None)
        w = tk[(tk.index > pd.Timestamp(t - timedelta(seconds=20)).tz_localize(None)) & (tk.index <= nv)] if tk is not None else None
        if w is not None and len(w):
            hi, last = float(w["price"].max()), float(w["price"].iloc[-1])
            lo_bid = float(w["best_bid"].min()) if w["best_bid"].notna().any() else None
        else:
            hi = last = lo_bid = None
        if hi is not None:
            peak = max(peak, W._net_return_pct(sym, avg, hi, qty))
        cur_net = W._net_return_pct(sym, avg, last, qty) if last is not None else None
        bid_net = W._net_return_pct(sym, avg, lo_bid, qty) if lo_bid is not None else None

        m = t.replace(second=0, microsecond=0)
        if cur_min != m:
            cur_min = m
            df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
            try:
                bars = MFF._prepare_bars(SE.resample_completed_3m(df1, now=t))
            except Exception:
                bars = None
            try:
                ser = MU.load_mu_1m(t, str(RT))
                if ser is not None:
                    ser = ser[ser.index + pd.Timedelta(minutes=1) <= MU._naive(t)]
                mu_pat = str(MU.evaluate_pattern(ser, dirn, m - timedelta(minutes=5)).get("mu_pattern_state"))
            except Exception:
                mu_pat = MU.PATTERN_UNAVAILABLE
        if mu_pat == MU.PATTERN_CONFIRM:
            mu_confirm = True

        s = dict.fromkeys(SIGS, False)
        try:
            gs = TWF._gap_series(bars) if bars is not None else None
            if gs is not None and len(gs) >= 2:
                s["s1_gap_shrink"] = (float(gs["gap"].iloc[-1]) * sgn) < (float(gs["gap"].iloc[-2]) * sgn)
        except Exception:
            pass
        s["s2_mu_weak"] = bool(mu_confirm and mu_pat in (MU.PATTERN_STALL, MU.PATTERN_DIVERGE))
        c = None
        if ctt is not None:
            wn = ctt[(ctt.index >= nv - timedelta(seconds=60)) & (ctt.index < nv)]
            c = float(wn.mean()) if len(wn) else None
        s["s3_cttr_weak"] = bool(XS.state_for(dirn, c) == XS.MISMATCH)
        s["s4_giveback"] = bool(cur_net is not None and (peak - cur_net) >= 0.5)
        try:
            if bars is not None and len(bars):
                vw = MFF._session_vwap(bars)
                v = float(vw.iloc[-1]) if len(vw) and pd.notna(vw.iloc[-1]) else None
                if v and v > 0:
                    c3 = float(bars["close"].iloc[-1])
                    lost = (c3 < v) if dirn == Direction.UP_RED else (c3 > v)
                    if not lost:
                        df1b = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)]
                        if len(df1b):
                            c1 = float(df1b["close"].iloc[-1])
                            lost = (c1 < v) if dirn == Direction.UP_RED else (c1 > v)
                    s["s5_vwap_lost"] = bool(lost)
        except Exception:
            pass
        ti = t.isoformat()
        s["s6_opp_pending"] = any(p0 <= ti < p1 and dd == opp_dir for p0, p1, dd in pend)

        n = sum(s.values())
        note = []
        base_line = line_for(peak)
        for v, need in (("W1", 1), ("W2", 2)):
            S = st[v]
            if S["out"] is None:
                if base_line is not None and not S["act"] and n >= need:
                    S["act"] = True
                    note.append(f"{v}활성[{','.join(k[1] for k in SIGS if s[k])}]")
                if S["act"] and base_line is not None and (S["line"] is None or base_line > S["line"]):
                    S["line"] = base_line
                if S["act"] and S["line"] is not None and bid_net is not None and bid_net <= S["line"]:
                    S["out"] = ti
                    note.append(f"**{v} 이탈 청산** (선 {S['line']}, bid {bid_net:.2f}%)")
        print(f"{t.strftime('%H:%M:%S'):10s}{peak:>8.2f}{(cur_net if cur_net is not None else float('nan')):>7.2f}"
              f"{(bid_net if bid_net is not None else float('nan')):>9.2f}  "
              + "".join(f"{'●' if s[k] else '·':>4s}" for k in SIGS)
              + f"{n:>3d}{(st['W1']['line'] if st['W1']['act'] else '-'):>6}"
              f"{(st['W2']['line'] if st['W2']['act'] else '-'):>6}  " + " ".join(note))
        t += timedelta(seconds=20)
    print(f"\n신호 범례: s1 MACD gap 축소 / s2 MU 약화 / s3 체결강도 약화 / "
          f"s4 고점 0.5%p 반납 / s5 VWAP 우위상실 / s6 반대플래그 미확정")
    for v in ("W1", "W2"):
        print(f"  {v}: 활성={st[v]['act']}  보호선={st[v]['line']}  이탈={st[v]['out'] or '없음'}")


if __name__ == "__main__":
    main()
