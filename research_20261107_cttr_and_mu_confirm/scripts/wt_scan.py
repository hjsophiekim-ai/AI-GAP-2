"""MU ENTRY_WAIT 모집단 — out_regime2.csv 에 WAIT 전용 피처를 붙인다 -> out_wait.csv

11/05·11/06 의 mu_status 는 신호원장에 비어 있어 MU assist 원장에서 채운다.
추가 피처: opp_gap(= max(0, −hy_gap_prev), production WAIT 게이트의 2번 조건), 누적 체결강도.
"""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X
import wx_lib as W
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")
LEDGER_DAYS = {"2026-11-05", "2026-11-06"}

ROWS = list(csv.DictReader(open("out_regime2.csv", encoding="utf-8-sig")))

# ── 11/05·11/06 mu_status / hy_gap_prev 를 MU assist 원장에서 보충 ──────────
for day in sorted(LEDGER_DAYS):
    ymd = day.replace("-", "")
    led = {}
    with open(W.LED_ROOT / ymd / "macd2_mu_assist_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r["kind"] != "ENTRY_SHADOW":
                continue
            fb = datetime.fromisoformat(r["flag_bar_at"]).astimezone(W.KST).strftime("%H:%M")
            led[(fb, r["direction"])] = r
    n = 0
    for row in ROWS:
        if row["day"] != day:
            continue
        s = led.get((row["flag_at"], row["dir"]))
        if s:
            row["mu_status"] = s["mu_entry_assist_status"]
            row["_gprev"] = s["hy_gap_prev"]
            n += 1
    print(f"{day}: MU assist 원장에서 {n}건 보충", file=sys.stderr)

WAIT = [r for r in ROWS if r["mu_status"] == "ENTRY_WAIT"]
print(f"ENTRY_WAIT {len(WAIT)}건", file=sys.stderr)

# ── 반대 gap / 누적 체결강도 보충 (RECON 날짜는 1분봉에서 재계산) ───────────
OUT = []
for day in sorted({r["day"] for r in WAIT}):
    sub = [r for r in WAIT if r["day"] == day]
    snap = X.DaySnap(day)
    need = [r for r in sub if not r.get("_gprev")]
    hy = W.load_1m(day, "hynix", prefer_snapshot=(day in LEDGER_DAYS)) if need else None
    for r in sub:
        d = Direction(r["dir"])
        flag = datetime.strptime(f"{day} {r['flag_at']}", "%Y-%m-%d %H:%M").replace(tzinfo=W.KST)
        gprev = r.get("_gprev")
        if not gprev and hy is not None:
            g = W.hy_gaps(hy, flag, d)
            gprev = g[0]
        gprev = None if gprev in (None, "") else float(gprev)
        t3 = (flag + timedelta(minutes=6)).replace(tzinfo=None)
        c = snap.cum(t3) if snap.ok else None
        r["opp_gap"] = "" if gprev is None else round(max(0.0, -gprev), 1)
        r["cttr"] = "" if c is None else round(c, 2)
        r["cttr_match"] = X.match(d, c)
        r.pop("_gprev", None)
        OUT.append(r)
    del snap, hy
    print("done", day, file=sys.stderr)

cols = [c for c in ROWS[0] if c != "_gprev"] + ["opp_gap", "cttr", "cttr_match"]
cols = list(dict.fromkeys(cols))
with open("out_wait.csv", "w", newline="", encoding="utf-8-sig") as fo:
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    for r in OUT:
        w.writerow({c: r.get(c, "") for c in cols})
print(f"out_wait.csv — {len(OUT)}건", file=sys.stderr)
