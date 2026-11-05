import csv, sys
sys.stdout.reconfigure(encoding="utf-8")
import io, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_o = sys.stdout
class _N(io.StringIO):
    def reconfigure(self, **k): pass
sys.stdout = _N()
from analyze import DAYS, bt, mu_status, cttr, direction_of, etf  # noqa
sys.stdout = _o
import collections
out = []; cnt = collections.Counter()
for d in DAYS:
    mu = mu_status(d)
    for r in csv.DictReader(open(bt(d) / "macd2_signal_ledger.csv", encoding="utf-8")):
        sid = r["signal_id"]
        if not (sid.startswith(d) and sid.endswith(":TW2_3SLOT_CONFIRM")) or r["order_result"] in ("EXECUTED", "WAITING"):
            continue
        st = (mu.get(sid) or {}).get("mu_entry_assist_status", "")
        c, cs = cttr(d, r["detected_at"][11:16], direction_of(sid))
        cnt[(st or "MU기록없음", cs)] += 1
        if st == "ENTRY_CONFIRM" and cs == "MATCH":
            sym = "0193T0" if direction_of(sid) == "UP_RED" else "0197X0"
            t0 = r["detected_at"][11:16]
            bars = [b for b in etf(d, sym) if t0 <= b["datetime"][11:16]]
            e = float(bars[0]["open"]) + 5 if bars else None
            seg = bars[:60]
            mfe = (max(float(b["high"]) for b in seg) / e - 1) * 100 if seg else None
            mae = (min(float(b["low"]) for b in seg) / e - 1) * 100 if seg else None
            out.append((d[4:], sid[9:24], t0, r["order_result"], r["block_reason"], c, mfe, mae))
print("막힌 T+3 후보의 MU상태×체결강도 분포:", dict(cnt))
print("STRONG 인데 막힌 후보:")
for x in out:
    print(f"  {x[0]} {x[1]} {x[2]} {x[3]} {x[4]} cttr={x[5]} | 이후60분 MFE {x[6]:+.2f}% MAE {x[7]:+.2f}%")
