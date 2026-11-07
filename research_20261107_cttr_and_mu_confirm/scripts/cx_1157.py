"""11/06 11:57 BLUE 상세 + production exec_strength 와의 패리티 확인."""
import csv
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

import cx_lib as X
import wx_lib as W
from app.trading.macd2 import exec_strength
from app.trading.macd2.models import Direction

DAY = "2026-11-06"
snap = X.DaySnap(DAY)

# production mean_cttr 은 파일 끝 256KB 만 읽는다(라이브 append 전제) -> 장중 과거 시각엔 None.
# 정의 동일성은 파일 끝(장 마감 직전) 시각에서 대조한다.
print("=== 패리티: cx B_prod60 vs exec_strength.mean_cttr (production 그대로) ===")
for hm in ("15:27", "15:28", "15:29"):
    end = datetime.fromisoformat(f"{DAY}T{hm}:00")
    mine = snap.mean_cttr(end, 60)
    prod = exec_strength.mean_cttr(end, str(W.rt_root(DAY)))
    print(f"  {hm}  cx={mine}  prod={prod}  일치={abs((mine or 0)-(prod or 0)) < 1e-9}")

print()
print("=== 11/06 11:57 BLUE — 판정시각(12:03) 전후 창 체결강도 추이 ===")
d = Direction.DOWN_BLUE
for off in (-9, -6, -3, 0, 3, 6, 9, 15):
    t = datetime.fromisoformat(f"{DAY}T12:03:00") + timedelta(minutes=off)
    a, f60, f180 = snap.cum(t), snap.flow(t, 60), snap.flow(t, 180)
    print(f"  {t:%H:%M}  누적={a}  60초창={f60} ({X.match(d, f60)})  "
          f"180초창={f180} ({X.match(d, f180)})  D={X.drift(d, f60, f180)}")

print()
print("=== 11/06 신호원장 해당 행 ===")
with open(W.LED_ROOT / "20261106" / "macd2_signal_ledger.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        if r.get("direction") != "DOWN_BLUE" or not r.get("flag_bar_at", "").count("11:57"):
            continue
        for k in ("signal_id", "flag_bar_at", "order_result", "block_reason",
                  "tw2_3slot_block_reason", "time_window_quality_score", "mu_entry_status",
                  "mu_wait_decision", "mu_wait_selective", "mu_wait_exec_strength_match",
                  "mu_wait_exec_strength_value", "mu_wait_opposite_gap",
                  "wait_xs_decision", "wait_xs_teg_override", "strategy_mode"):
            if k in r:
                print(f"  {k:32s} = {r[k]!r}")

print()
print("=== 하이닉스/인버스 1분봉 (11:50~12:35) ===")
hy = W.load_1m(DAY, "hynix", prefer_snapshot=True)
iv = W.load_1m(DAY, "inverse", prefer_snapshot=True)
for df, nm in ((hy, "하이닉스"), (iv, "인버스")):
    seg = df[(df["datetime"] >= f"{DAY} 11:50+09:00") & (df["datetime"] <= f"{DAY} 12:35+09:00")]
    s = " ".join(f"{r.datetime:%H:%M}={r.close:g}" for r in seg.itertuples() if r.datetime.minute % 5 == 0)
    print(f"  {nm}: {s}")
