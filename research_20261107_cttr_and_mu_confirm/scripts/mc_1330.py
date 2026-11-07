"""11/06 13:30 UP_RED (MU ENTRY_CONFIRM, 예수금 부족으로 미진입) 의 사후 경로."""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X  # noqa: F401  (경로 설정)
import wx_lib as W

sys.stdout.reconfigure(encoding="utf-8")
DAY = "2026-11-06"

print("=== MU assist 원장 (ENTRY_SHADOW) ===")
with open(W.LED_ROOT / "20261106" / "macd2_mu_assist_ledger.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        if r["kind"] != "ENTRY_SHADOW":
            continue
        t = datetime.fromisoformat(r["flag_bar_at"]).astimezone(W.KST)
        print(f"  {t:%H:%M} {r['direction']:10s} {r['mu_entry_assist_status']:14s} "
              f"pat={r['mu_pattern_state']:10s} hy={r['hy_gap_t2_state']:7s} "
              f"prev={r['hy_gap_prev']:>8s} flag={r['hy_gap_flag']:>8s} t2={r['hy_gap_t2_bar']:>8s}")

print()
print("=== 13:30 UP_RED 신호원장 상세 ===")
with open(W.LED_ROOT / "20261106" / "macd2_signal_ledger.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        if not r["signal_id"].startswith("20261106_133000_UP_RED:"):
            continue
        for k, v in r.items():
            if v not in ("", None):
                print(f"  {k:36s} = {v!r}")

print()
print("=== 사후 경로 (레버리지 ETF, T+3 13:36 종가 진입 가정) ===")
etf = W.load_1m(DAY, "long", prefer_snapshot=True)
t3 = datetime.fromisoformat(f"{DAY}T13:36:00+09:00")
f = W.forward(etf, t3, horizon_min=(5, 10, 15, 30, 60, 120))
print(f"  진입가 {f['entry_px']}  " + "  ".join(f"{h}분 {f['r'+str(h)]:+.3f}%" for h in (5, 10, 15, 30, 60, 120)
                                               if f.get("r" + str(h)) is not None))
print(f"  ±1.5% 선터치: {f['touch']}")
fut = etf[etf["datetime"] >= t3]
e = f["entry_px"]
print(f"  MFE {(fut['high'].max()/e-1)*100:+.3f}%   MAE {(fut['low'].min()/e-1)*100:+.3f}%")
print("  1분봉: " + " ".join(f"{r.datetime:%H:%M}={ (r.close/e-1)*100:+.2f}%"
                            for r in fut.itertuples() if r.datetime.minute % 5 == 0))
