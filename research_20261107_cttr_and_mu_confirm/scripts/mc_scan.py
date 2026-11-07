"""MU ENTRY_CONFIRM 후보 전수 + 사후 경로 — 날짜별 순차, 하루치만 메모리에 올린다.

MU ENTRY_CONFIRM = mu_pattern CONFIRM ∧ 하이닉스 gap EXPAND (mu_assist.entry_assist_status).
  10/19~11/04 : 1분봉에서 production 함수로 플래그/gap/MU 재계산 (RECON — 원장 없음)
  11/05·11/06 : production MU assist 원장 + 신호원장 그대로
"""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X
import wx_lib as W
from app.trading.macd2 import mu_assist, mu_catchup
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")

RECON_DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
              "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
              "2026-11-02", "2026-11-03", "2026-11-04"]
LEDGER_DAYS = ["2026-11-05", "2026-11-06"]

COLS = ["day", "flag_at", "t3_at", "dir", "src", "session", "flag_seq", "since_opp_min",
        "g_prev", "g_flag", "g_t2", "opp_gap", "gap_growth",
        "mu_post2_bp", "mu_acc2", "mu_pre3_bp",
        "mu_session_pct", "hy_session_pct", "mu_hy_gap",
        "cttr", "cttr_match", "hy_pos_in_day", "hy_day_range_pct",
        "prod_result", "prod_block",
        "entry_px", "r15", "r30", "mfe", "mae", "touch"]


def day_pos(hy, t3):
    """T+3 까지의 당일 하이닉스 고저 범위 안 위치(0=저가, 1=고가)와 범위 폭."""
    seg = hy[(hy["datetime"] + __import__("pandas").Timedelta(minutes=1) <= t3)
             & (hy["datetime"].dt.hour >= 9)]
    if seg.empty:
        return "", ""
    hi, lo, c = float(seg["high"].max()), float(seg["low"].min()), float(seg["close"].iloc[-1])
    if hi <= lo:
        return "", ""
    return round((c - lo) / (hi - lo), 3), round((hi / lo - 1) * 100, 2)


def build(day, snap, flags, getters, w):
    """flags = [(flag_dt, Direction, extra dict)] 순서대로. getters = 날짜별 공용 자원."""
    hy, mus = getters["hy"], getters["mu"]
    etfs = {}
    prev_dir = {}
    for seq, (dt, d, extra) in enumerate(flags, 1):
        last_opp = prev_dir.get(Direction.DOWN_BLUE if d == Direction.UP_RED else Direction.UP_RED)
        prev_dir[d] = dt
        if extra["status"] != mu_assist.ENTRY_CONFIRM:
            continue
        t3 = dt + timedelta(minutes=6)
        g = extra["gaps"]
        pat = mu_assist.evaluate_pattern(mus, d, dt)
        cu = mu_catchup.evaluate(d, dt, t3, hy, mu_series=mus,
                                 cttr={"cttr": None, "state": "UNAVAILABLE"})
        k = "long" if d == Direction.UP_RED else "inverse"
        if k not in etfs:
            etfs[k] = W.load_1m(day, k, prefer_snapshot=(day in LEDGER_DAYS))
        c = snap.cum(t3.replace(tzinfo=None))
        pos, rng = day_pos(hy, t3)
        row = {"day": day, "flag_at": f"{dt:%H:%M}", "t3_at": f"{t3:%H:%M}", "dir": d.value,
               "src": extra["src"], "session": "AM" if t3.time() < __import__("datetime").time(11) else "PM",
               "flag_seq": seq,
               "since_opp_min": "" if last_opp is None else int((dt - last_opp).total_seconds() // 60),
               "g_prev": round(g[0], 1) if g[0] is not None else "",
               "g_flag": round(g[1], 1) if g[1] is not None else "",
               "g_t2": round(g[2], 1) if g[2] is not None else "",
               "opp_gap": round(max(0.0, -g[0]), 1) if g[0] is not None else "",
               "gap_growth": round(g[2] - g[1], 1) if (g[1] is not None and g[2] is not None) else "",
               "mu_post2_bp": pat["mu_post2_bp"], "mu_acc2": pat["mu_acc2"],
               "mu_pre3_bp": pat["mu_pre3_return_bp"],
               **{k: (extra["sess"][k] if extra.get("sess", {}).get(k) not in (None, "")
                      else ("" if cu[k] is None else cu[k]))
                  for k in ("mu_session_pct", "hy_session_pct", "mu_hy_gap")},
               "cttr": "" if c is None else round(c, 2),
               "cttr_match": X.match(d, c), "hy_pos_in_day": pos, "hy_day_range_pct": rng,
               "prod_result": extra.get("prod_result", ""), "prod_block": extra.get("prod_block", "")}
        f = W.forward(etfs[k], t3, horizon_min=(15, 30))
        row.update(f)
        if f:
            fut = etfs[k][etfs[k]["datetime"] >= t3]
            e = f["entry_px"]
            row["mfe"] = round((float(fut["high"].max()) / e - 1) * 100, 3)
            row["mae"] = round((float(fut["low"].min()) / e - 1) * 100, 3)
        w.writerow({c2: row.get(c2, "") for c2 in COLS})


def flags_recon(day, hy, mus):
    out = []
    for dt, d in W.flags_of_day(hy, day):
        g = W.hy_gaps(hy, dt, d)
        st, _, _ = W.mu_status(mus, d, dt, g)
        out.append((dt, d, {"status": st, "gaps": g, "src": "RECON"}))
    return out


def flags_ledger(day):
    ymd = day.replace("-", "")
    led = W.LED_ROOT / ymd
    sig = {}
    with open(led / "macd2_signal_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            sig[r["signal_id"]] = r
    out = []
    with open(led / "macd2_mu_assist_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r["kind"] != "ENTRY_SHADOW":
                continue
            d = Direction(r["direction"])
            dt = datetime.fromisoformat(r["flag_bar_at"]).astimezone(W.KST)
            g = tuple(float(r[k]) if r[k] else None
                      for k in ("hy_gap_prev", "hy_gap_flag", "hy_gap_t2_bar"))
            s = sig.get(r["signal_id"] + ":TW2_3SLOT_CONFIRM", sig.get(r["signal_id"], {}))
            out.append((dt, d, {"status": r["mu_entry_assist_status"], "gaps": g, "src": "LEDGER",
                                "prod_result": s.get("order_result", ""),
                                "prod_block": s.get("block_reason", ""),
                                # 세션 지표는 원장값 우선 — 스냅샷 재구성 1분봉은 08:00 기준봉이 틀어진다
                                "sess": {k: s.get(k, "") for k in
                                         ("mu_session_pct", "hy_session_pct", "mu_hy_gap")}}))
    out.sort(key=lambda x: x[0])
    return out


if __name__ == "__main__":
    with open("out_confirm.csv", "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.DictWriter(fo, fieldnames=COLS)
        w.writeheader()
        for day in RECON_DAYS + LEDGER_DAYS:
            snap = X.DaySnap(day)
            if not snap.ok:
                print(day, "스냅샷 없음", file=sys.stderr)
                continue
            led = day in LEDGER_DAYS
            hy = W.load_1m(day, "hynix", prefer_snapshot=led)
            mus = W.mu_series(day)
            if hy.empty or mus is None:
                print(day, "1분봉/MU 없음", file=sys.stderr)
                continue
            flags = flags_ledger(day) if led else flags_recon(day, hy, mus)
            build(day, snap, flags, {"hy": hy, "mu": mus}, w)
            del snap, hy, mus, flags
            fo.flush()
            print("done", day, file=sys.stderr)
