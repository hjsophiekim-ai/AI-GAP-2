"""11:00 이후 BLUE 후보 × 체결강도 A~D — 날짜별 순차, 하루치만 메모리에 올린다.

후보: 방향 DOWN_BLUE ∧ T+3 ≥ 11:00 ∧ MU 상태 ∈ {ENTRY_WAIT, ENTRY_CONFIRM}.
  10/19~11/04 : 1분봉에서 production 함수로 플래그/HY gap/MU 재계산 (RECON — 원장 없음).
  11/05·11/06 : production MU assist 원장 + 신호원장을 그대로 읽는다.
"""
import csv
import sys
from datetime import datetime, time, timedelta

import cx_lib as X
import wx_lib as W
from app.trading.macd2 import mu_assist
from app.trading.macd2.models import Direction

RECON_DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
              "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
              "2026-11-02", "2026-11-03", "2026-11-04"]
LEDGER_DAYS = ["2026-11-05", "2026-11-06"]
OK_MU = (mu_assist.ENTRY_WAIT, mu_assist.ENTRY_CONFIRM)
CUTOFF = time(11, 0)

COLS = ["day", "flag_at", "t3_at", "src", "mu_status", "hy_state", "opp_gap",
        "A_cum", "A_match", "B_prod60", "B_match", "F60", "F60_match",
        "C180", "C_match", "F180", "F180_match", "D_drift",
        "low_quality", "prod_result", "prod_block", "prod_wait_decision",
        "entry_px", "r15", "r30", "touch"]


def row_for(day, snap, dt, t3, mu_st, hy_st, opp, etf):
    d = Direction.DOWN_BLUE
    end = t3.replace(tzinfo=None)
    a = snap.cum(end)
    b = snap.mean_cttr(end, 60)
    c = snap.mean_cttr(end, 180)
    f60, f180 = snap.flow(end, 60), snap.flow(end, 180)
    r = {"day": day, "flag_at": f"{dt:%H:%M}", "t3_at": f"{t3:%H:%M}",
         "mu_status": mu_st, "hy_state": hy_st,
         "opp_gap": "" if opp is None else round(opp, 1),
         "A_cum": "" if a is None else round(a, 2), "A_match": X.match(d, a),
         "B_prod60": "" if b is None else round(b, 2), "B_match": X.match(d, b),
         "F60": "" if f60 is None else f60, "F60_match": X.match(d, f60),
         "C180": "" if c is None else round(c, 2), "C_match": X.match(d, c),
         "F180": "" if f180 is None else f180, "F180_match": X.match(d, f180),
         "D_drift": X.drift(d, f60, f180)}
    r.update(W.forward(etf, t3))
    return r


def scan_recon(w, day, snap):
    bars = W.load_1m(day, "hynix")
    if bars.empty:
        print(day, "하이닉스 1분봉 없음", file=sys.stderr)
        return
    mus = W.mu_series(day)
    if mus is None:
        print(day, "MU 1분봉 없음", file=sys.stderr)
        return
    etf = None
    for dt, d in W.flags_of_day(bars, day):
        if d != Direction.DOWN_BLUE:
            continue
        t3 = dt + timedelta(minutes=6)
        if t3.time() < CUTOFF:
            continue
        g = W.hy_gaps(bars, dt, d)
        st, _, hy = W.mu_status(mus, d, dt, g)
        if st not in OK_MU:
            continue
        if etf is None:
            etf = W.load_1m(day, "inverse")
        opp = None if g[0] is None else max(0.0, -g[0])
        r = row_for(day, snap, dt, t3, st, hy, opp, etf)
        r.update({"src": "RECON", "low_quality": "NA"})
        w.writerow({k: r.get(k, "") for k in COLS})


def scan_ledger(w, day, snap):
    ymd = day.replace("-", "")
    led = W.LED_ROOT / ymd
    sig = {}
    with open(led / "macd2_signal_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            sig[r["signal_id"]] = r
    etf = None
    with open(led / "macd2_mu_assist_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r["kind"] != "ENTRY_SHADOW" or r["direction"] != Direction.DOWN_BLUE.value:
                continue
            if r["mu_entry_assist_status"] not in OK_MU:
                continue
            dt = datetime.fromisoformat(r["flag_bar_at"]).astimezone(W.KST)
            t3 = dt + timedelta(minutes=6)
            if t3.time() < CUTOFF:
                continue
            if etf is None:
                etf = W.load_1m(day, "inverse", prefer_snapshot=True)
            opp = None if not r["hy_gap_prev"] else max(0.0, -float(r["hy_gap_prev"]))
            s = sig.get(r["signal_id"], {})
            blocks = (s.get("block_reason", ""), s.get("tw2_3slot_block_reason", ""))
            row = row_for(day, snap, dt, t3, r["mu_entry_assist_status"],
                          r["hy_gap_t2_state"], opp, etf)
            row.update({"src": "LEDGER",
                        "low_quality": "Y" if any("LOW_QUALITY" in b for b in blocks) else "N",
                        "prod_result": s.get("order_result", ""),
                        "prod_block": blocks[0] or blocks[1],
                        "prod_wait_decision": s.get("mu_wait_decision", "")})
            w.writerow({k: row.get(k, "") for k in COLS})


if __name__ == "__main__":
    with open("out_blue.csv", "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.DictWriter(fo, fieldnames=COLS)
        w.writeheader()
        for day in RECON_DAYS + LEDGER_DAYS:
            snap = X.DaySnap(day)
            if not snap.ok:
                print(day, "스냅샷 없음", file=sys.stderr)
                continue
            (scan_ledger if day in LEDGER_DAYS else scan_recon)(w, day, snap)
            del snap
            fo.flush()
            print("done", day, file=sys.stderr)
