"""MU CONFIRM + WAIT 통합 모집단 + 왕복장 후보 지표 6종 -> out_mu.csv

왕복장 후보(전부 T+3 이전 데이터로만, 미래 데이터 없음):
  C1 er_day     09:00~T+3 효율비 |순변화|/Σ|1분변화|           (낮을수록 왕복)
  C2 er_60      최근 60분 효율비                                (낮을수록 왕복)
  C3 nfb        그날 이전 플래그 수                              (높을수록 왕복)
  C4 flag_rate  nfb / 경과시간(h) — 시각 보정판                  (높을수록 왕복)
  C5 opp_min    직전 반대방향 플래그 경과분                       (짧을수록 왕복)
  C6 range_pos  |순변화| / (당일 고저폭)                          (낮을수록 왕복)
"""
import csv
import sys
from datetime import datetime, time, timedelta

import cx_lib as X  # noqa: F401
import pandas as pd
import wx_lib as W
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")
LEDGER_DAYS = {"2026-11-05", "2026-11-06"}
OPEN = time(9, 0)

BASE = list(csv.DictReader(open("out_regime2.csv", encoding="utf-8-sig")))

# 11/05·11/06 mu_status 보충 (신호원장엔 비어 있다)
for day in sorted(LEDGER_DAYS):
    led = {}
    with open(W.LED_ROOT / day.replace("-", "") / "macd2_mu_assist_ledger.csv",
              encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r["kind"] == "ENTRY_SHADOW":
                fb = datetime.fromisoformat(r["flag_bar_at"]).astimezone(W.KST).strftime("%H:%M")
                led[(fb, r["direction"])] = r["mu_entry_assist_status"]
    for row in BASE:
        if row["day"] == day and (row["flag_at"], row["dir"]) in led:
            row["mu_status"] = led[(row["flag_at"], row["dir"])]

POP = [r for r in BASE if r["mu_status"] in ("ENTRY_CONFIRM", "ENTRY_WAIT")]
print(f"MU CONFIRM+WAIT {len(POP)}건", file=sys.stderr)

COLS = list(BASE[0]) + ["er_60", "flag_rate", "range_pos", "elapsed_min"]
COLS = list(dict.fromkeys(COLS))


def er_of(c):
    if len(c) < 5:
        return None
    d = c.diff().abs().sum()
    if d <= 0:
        return None
    return round(abs(float(c.iloc[-1]) - float(c.iloc[0])) / float(d), 4)


out = []
for day in sorted({r["day"] for r in POP}):
    sub = [r for r in POP if r["day"] == day]
    hy = W.load_1m(day, "hynix", prefer_snapshot=(day in LEDGER_DAYS))
    open_t = datetime.fromisoformat(f"{day}T09:00:00+09:00")
    for r in sub:
        t3 = datetime.strptime(f"{day} {r['t3_at']}", "%Y-%m-%d %H:%M").replace(tzinfo=W.KST)
        day_seg = hy[(hy["datetime"] >= open_t) & (hy["datetime"] + pd.Timedelta(minutes=1) <= t3)]
        c_day = pd.to_numeric(day_seg["close"], errors="coerce").dropna()
        w60 = hy[(hy["datetime"] >= t3 - timedelta(minutes=60))
                 & (hy["datetime"] + pd.Timedelta(minutes=1) <= t3)]
        c60 = pd.to_numeric(w60["close"], errors="coerce").dropna()
        elapsed = max(1.0, (t3 - open_t).total_seconds() / 60.0)
        nfb = float(r["n_flag_before"])
        rp = None
        if len(c_day) >= 5:
            hi = float(pd.to_numeric(day_seg["high"], errors="coerce").max())
            lo = float(pd.to_numeric(day_seg["low"], errors="coerce").min())
            if hi > lo:
                rp = round(abs(float(c_day.iloc[-1]) - float(c_day.iloc[0])) / (hi - lo), 4)
        r["er_60"] = "" if er_of(c60) is None else er_of(c60)
        r["flag_rate"] = round(nfb / (elapsed / 60.0), 3)
        r["range_pos"] = "" if rp is None else rp
        r["elapsed_min"] = int(elapsed)
        out.append({c: r.get(c, "") for c in COLS})
    del hy
    print("done", day, file=sys.stderr)

with open("out_mu.csv", "w", newline="", encoding="utf-8-sig") as fo:
    w = csv.DictWriter(fo, fieldnames=COLS)
    w.writeheader()
    w.writerows(out)
print(f"out_mu.csv — {len(out)}건", file=sys.stderr)
