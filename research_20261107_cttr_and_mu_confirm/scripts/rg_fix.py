"""11/05·11/06 행을 production 원장 기준으로 다시 만든다 -> out_regime2.csv

스냅샷 재구성 1분봉은 플래그 시각과 08:00 기준봉이 어긋난다(11/06 13:36 괴리 부호가 뒤집혔다).
그 두 날은 플래그 시각·방향·괴리를 **신호원장에서 그대로** 읽는다.
효율비(er)와 사후경로는 가격경로라 스냅샷 1분봉으로 계산해도 된다.
"""
import csv
import sys
from datetime import datetime, time, timedelta

import cx_lib as X  # noqa: F401
import pandas as pd
import wx_lib as W
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")
LEDGER_DAYS = ["2026-11-05", "2026-11-06"]
COLS = None

keep = []
for r in csv.DictReader(open("out_regime.csv", encoding="utf-8-sig")):
    if COLS is None:
        COLS = list(r)
    if r["day"] not in LEDGER_DAYS:
        keep.append(r)
print(f"RECON 유지 {len(keep)}건 (10/19~11/04)")


def day_er(hy, start, end, sign=1.0):
    seg = hy[(hy["datetime"] >= start) & (hy["datetime"] + pd.Timedelta(minutes=1) <= end)]
    c = pd.to_numeric(seg["close"], errors="coerce").dropna()
    if len(c) < 5:
        return None, None
    diff = c.diff().abs().sum()
    if diff <= 0:
        return None, None
    net = float(c.iloc[-1]) - float(c.iloc[0])
    return round(abs(net) / float(diff), 4), round(sign * net / float(c.iloc[0]) * 100, 3)


new = []
for day in LEDGER_DAYS:
    ymd = day.replace("-", "")
    best = {}
    for r in csv.DictReader(open(W.LED_ROOT / ymd / "macd2_signal_ledger.csv", encoding="utf-8-sig")):
        fb = (r.get("signal_bar_at") or "")[11:16] or (r.get("time_window_flag_bar_at") or "")[11:16]
        if not fb or r["direction"] not in ("UP_RED", "DOWN_BLUE"):
            continue
        k = (fb, r["direction"])
        if k not in best or r.get("mu_hy_gap"):
            best[k] = r
    flags = sorted(best.items())
    hy = W.load_1m(day, "hynix", prefer_snapshot=True)
    open_t = datetime.fromisoformat(f"{day}T09:00:00+09:00")
    close_t = datetime.fromisoformat(f"{day}T15:30:00+09:00")
    etfs = {}
    seq = 0
    prev_dir = {}
    for (fb, dr), r in flags:
        flag = datetime.fromisoformat(f"{day}T{fb}:00+09:00")
        if flag.time() < time(9, 0):            # 장전 플래그는 모집단에서 제외(진입 대상 아님)
            continue
        seq += 1
        d = Direction(dr)
        last_opp = prev_dir.get(Direction.DOWN_BLUE if d == Direction.UP_RED else Direction.UP_RED)
        prev_dir[d] = flag
        t3 = flag + timedelta(minutes=6)
        if t3 > close_t:
            continue
        sign = 1.0 if d == Direction.UP_RED else -1.0
        er, net = day_er(hy, open_t, t3, sign)
        k = "long" if d == Direction.UP_RED else "inverse"
        if k not in etfs:
            etfs[k] = W.load_1m(day, k, prefer_snapshot=True)
        row = {"day": day, "flag_at": fb, "t3_at": f"{t3:%H:%M}", "dir": dr,
               "session": "AM" if t3.time() < time(11, 0) else "PM",
               "mu_status": r.get("mu_entry_status", ""),
               "mu_session_pct": r.get("mu_session_pct", ""),
               "hy_session_pct": r.get("hy_session_pct", ""),
               "mu_hy_gap": r.get("mu_hy_gap", ""),
               "er": "" if er is None else er, "net_pct": "" if net is None else net,
               "n_flag_before": seq - 1, "flag_seq": seq,
               "since_opp_min": "" if last_opp is None else int((flag - last_opp).total_seconds() // 60),
               "prev_er": ""}
        f = W.forward(etfs[k], t3, horizon_min=(15, 30))
        row.update(f)
        if f:
            fut = etfs[k][etfs[k]["datetime"] >= t3]
            e = f["entry_px"]
            row["mfe"] = round((float(fut["high"].max()) / e - 1) * 100, 3)
            row["mae"] = round((float(fut["low"].min()) / e - 1) * 100, 3)
        new.append({c: row.get(c, "") for c in COLS})
    print(f"{day}: 원장 플래그 {seq}건 -> {len([x for x in new if x['day']==day])}건 기록")
    del hy, etfs

with open("out_regime2.csv", "w", newline="", encoding="utf-8-sig") as fo:
    w = csv.DictWriter(fo, fieldnames=COLS)
    w.writeheader()
    w.writerows(keep + new)
print(f"out_regime2.csv — 총 {len(keep)+len(new)}건")
