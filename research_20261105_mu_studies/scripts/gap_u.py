"""반대 gap 구간 × MU 상태(WAIT/CONFIRM/CATCH-UP/BAD) — 모든 T+3 후보의 표준화 결과 (U자형 확인).

후보: 현행 A(e494897) 재생 원장의 TW2_3SLOT_CONFIRM 판정(실제 진입 여부 무관).
gap : MU 기록 hy_gap_prev(방향부호) -> 반대 gap = max(0, -g_prev)  (production wait_selective_decision 과 같은 정의)
결과: 판정 시각 ETF 1분봉 시가+1틱 진입, +1.5% 고가 터치 익절 / 완성 3분봉 종가 -1.5% 손절 / 60분 종가 청산.
"""
import csv, io, os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _N(io.StringIO):
    def reconfigure(self, **k):
        pass


_o = sys.stdout; sys.stdout = _N()
from analyze import DAYS, bt, mu_status, cttr, direction_of, etf  # noqa: E402
sys.stdout = _o
sys.stdout.reconfigure(encoding="utf-8")

BUCKETS = [(0, 400, "0~400"), (400, 800, "400~800"), (800, 1200, "800~1200"), (1200, 1e18, "1200+")]


def outcome(d, sym, t0):
    bars = [b for b in etf(d, sym) if b["datetime"][11:16] >= t0][:60]
    if not bars:
        return None
    e = float(bars[0]["open"]) + 5
    start = datetime.strptime(d + bars[0]["datetime"][11:16], "%Y%m%d%H:%M")
    grp = {}
    for b in bars:
        t = datetime.strptime(d + b["datetime"][11:16], "%Y%m%d%H:%M")
        if float(b["high"]) >= e * 1.015:
            return 1.5, "TP"
        k = (t - start).seconds // 180
        grp[k] = float(b["close"])
        if t.minute % 3 == 2 and k > 0 and grp[k] <= e * 0.985:     # 진입봉 제외, 3분봉 완성 시점
            return round((grp[k] / e - 1) * 100, 2), "SL"
    return round((float(bars[-1]["close"]) / e - 1) * 100, 2), "TIME"


rows = []
for d in DAYS:
    mu = mu_status(d)
    seen = {}
    for r in csv.DictReader(open(bt(d) / "macd2_signal_ledger.csv", encoding="utf-8")):
        sid = r["signal_id"]
        if not (sid.startswith(d) and sid.endswith(":TW2_3SLOT_CONFIRM")):
            continue
        s = seen.setdefault(sid, {"cu": False, "exec": False, "first": r, "block": ""})
        s["cu"] |= r.get("mu_catchup_pass") == "PASS"
        s["exec"] |= r["order_result"] == "EXECUTED"
        if r["order_result"] not in ("WAITING",):
            s["block"] = s["block"] or r["block_reason"]
    for r2 in csv.DictReader(open(bt(d) / "macd2_signal_ledger.csv", encoding="utf-8")):
        if r2["signal_id"].startswith(d) and ":TW2_3SLOT_CONFIRM:E_BRK" in r2["signal_id"] and r2["order_result"] == "EXECUTED":
            seen.get(r2["signal_id"].split(":E_BRK")[0], {})["exec"] = True
    for sid, s in seen.items():
        m = mu.get(sid) or {}
        st = m.get("mu_entry_assist_status", "")
        try:
            gp = float(m.get("hy_gap_prev"))
        except (TypeError, ValueError):
            gp = None
        opp = None if gp is None else max(0.0, -gp)
        t0 = s["first"]["detected_at"][11:16]
        dr = direction_of(sid)
        c, cs = cttr(d, t0, dr)
        cat = "CATCH-UP" if s["cu"] else {"ENTRY_CONFIRM": "CONFIRM", "ENTRY_WAIT": "WAIT", "ENTRY_BAD": "BAD"}.get(st, "MU불가")
        oc = outcome(d, "0193T0" if dr == "UP_RED" else "0197X0", t0)
        if oc is None:
            continue
        rows.append(dict(d=d, sid=sid[9:24], t=t0, cat=cat, opp=opp, cs=cs, ret=oc[0], how=oc[1],
                         exe=s["exec"], block=s["block"], dir="RED" if dr == "UP_RED" else "BLUE"))


def line(rs):
    if not rs:
        return "-"
    w = [x["ret"] for x in rs if x["ret"] > 0]; l = [x["ret"] for x in rs if x["ret"] <= 0]
    pf = sum(w) / -sum(l) if l and sum(l) < 0 else float("inf")
    return (f"{len(rs):>3}건 승률 {len(w)/len(rs)*100:>4.0f}% 합 {sum(x['ret'] for x in rs):+6.2f}%p "
            f"평균 {sum(x['ret'] for x in rs)/len(rs):+.2f}% PF {pf:5.2f} TP {sum(1 for x in rs if x['how']=='TP')}"
            f"/SL {sum(1 for x in rs if x['how']=='SL')}")


print(f"후보 {len(rows)}건 (gap 없음 {sum(1 for x in rows if x['opp'] is None)}건 제외하고 구간 집계)\n")
for cat in ("WAIT", "CONFIRM", "CATCH-UP", "BAD"):
    print(f"[{cat}]")
    for lo, hi, lab in BUCKETS:
        rs = [x for x in rows if x["cat"] == cat and x["opp"] is not None and lo <= x["opp"] < hi]
        print(f"   {lab:<9} {line(rs)}")
    if cat == "WAIT":
        for lo, hi, lab in BUCKETS:
            rs = [x for x in rows if x["cat"] == cat and x["cs"] == "MATCH" and x["opp"] is not None and lo <= x["opp"] < hi]
            print(f"   {lab:<9} (체결강도 일치만) {line(rs)}")
print("\n[전체 구간별]")
for lo, hi, lab in BUCKETS:
    print(f"   {lab:<9} {line([x for x in rows if x['opp'] is not None and lo <= x['opp'] < hi])}")
print("\n[전수]")
for x in sorted(rows, key=lambda x: (x["cat"], x["opp"] if x["opp"] is not None else -1)):
    print(f"  {x['cat']:<8} gap {('-' if x['opp'] is None else f'{x[chr(111)+chr(112)+chr(112)]:7.1f}')} {x['d'][4:]} {x['sid']} {x['t']} {x['dir']:<4} "
          f"cttr {x['cs']:<8} -> {x['ret']:+.2f}% {x['how']:<4} {'[실진입]' if x['exe'] else x['block']}")
