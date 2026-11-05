"""MU STRONG 연구 1단계 — 현행 A(e494897, CHOP+MU+XS) 재생 원장에서
 (가) 소프트 사유로 막힌 후보의 STRONG 여부, (나) 실제 체결 거래의 MU 그룹별 성과."""
import csv, json, sys
from datetime import datetime, timedelta
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad/wt_study")
from compare import episodes  # noqa: E402

S46 = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/46f5b540-1e0b-4dc8-b0bf-faf804cf00b0/scratchpad")
SBB = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad")
# MU 정상 기간: 10/16(구형식, MU 판독불가) 제외. 11/02·11/03 은 9시대만 공백 -> 포함하되 표시.
DAYS = "20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030 20261102 20261103".split()
SOFT = {"TW2_REJECT_VWAP_VETO", "TW2_REJECT_RECENT_CROSSES", "REJECT_SHORT_FLAG_INTERVAL",
        "TW_TEG_3SLOT_REJECT_CHOP_TEG", "TW2_3SLOT_REJECT_TEG"}
_cache = {}


def snaps(d):
    if d in _cache:
        return _cache[d]
    root = SBB / "mu/rt" if d == "20261103" else S46 / "mu_sp/rt"
    p = root / "snapshots" / f"{d[:4]}-{d[4:6]}-{d[6:]}" / "000660.csv"
    rows = []
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("cttr"):
                    rows.append((r["ts"][:19], float(r["cttr"])))
    _cache[d] = rows
    return rows


def cttr(d, hhmm, direction):
    end = datetime.strptime(d + hhmm, "%Y%m%d%H:%M")
    a, b = (end - timedelta(seconds=60)).isoformat(), end.isoformat()
    v = [c for t, c in snaps(d) if a <= t < b]
    if not v:
        return None, "UNAVAIL"
    c = sum(v) / len(v)
    return round(c, 1), ("MATCH" if (c > 100) == (direction == "UP_RED") else "MISMATCH")


def bt(d):
    return (SBB if d == "20261103" else S46) / "rsp/bt" / f"FX_{d}" / "test_wk20"


def mu_status(d):
    p = bt(d) / "macd2_mu_assist_ledger.csv"
    out = {}
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8")):
            if r.get("kind") == "ENTRY_SHADOW":
                out[r["signal_id"]] = r
    return out


def direction_of(sid):
    return "UP_RED" if "_UP_RED" in sid else "DOWN_BLUE"


def etf(d, sym):
    tag = "long" if sym == "0193T0" else "inverse"
    for root in (S46 / "rsp/data", SBB / "rsp/data", SBB / "data"):
        p = root / f"replay_{d}_{tag}_1m.csv"
        if p.exists():
            return list(csv.DictReader(open(p, encoding="utf-8")))
    return []


print("== (가) 소프트 사유로 막힌 후보의 STRONG 여부")
blocked_strong = []
for d in DAYS:
    mu = mu_status(d)
    for r in csv.DictReader(open(bt(d) / "macd2_signal_ledger.csv", encoding="utf-8")):
        sid = r["signal_id"]
        if sid.startswith(d) and sid.endswith(":TW2_3SLOT_CONFIRM") and r["block_reason"] in SOFT:
            st = (mu.get(sid) or {}).get("mu_entry_assist_status", "(MU기록없음)")
            c, cs = cttr(d, r["detected_at"][11:16], direction_of(sid))
            strong = st == "ENTRY_CONFIRM" and cs == "MATCH"
            print(f"  {d[4:]} {sid[9:24]} {r['detected_at'][11:16]} {r['block_reason']:<28} MU={st:<14} cttr={c} {cs}"
                  + ("  <== STRONG" if strong else ""))
            if strong:
                blocked_strong.append((d, sid, r["block_reason"]))
print(f"  STRONG 인데 소프트 사유로 막힌 후보: {len(blocked_strong)}건 {blocked_strong}")

print("\n== (나) 실제 체결 거래 MU 그룹별")
trades = []
for d in DAYS:
    js = json.loads((SBB / "out_fx" / f"FX_{d}.json").read_text(encoding="utf-8"))
    eps = episodes(js, d)
    mu = mu_status(d)
    execd = [r for r in csv.DictReader(open(bt(d) / "macd2_signal_ledger.csv", encoding="utf-8"))
             if r["signal_id"].startswith(d) and r["order_result"] == "EXECUTED" and "TW2_3SLOT_CONFIRM" in r["signal_id"]]
    for e in eps:
        cand = [r for r in execd if r["detected_at"][11:19] <= e["t"]]
        r = cand[-1] if cand else None
        sid = r["signal_id"].split(":E_BRK")[0] if r else ""
        st = (mu.get(sid) or {}).get("mu_entry_assist_status", "")
        c, cs = cttr(d, r["detected_at"][11:16], direction_of(sid)) if r else (None, "")
        wait = (r or {}).get("mu_wait_decision", "")
        if st == "ENTRY_CONFIRM" and cs == "MATCH":
            g = "1_STRONG"
        elif st == "ENTRY_CONFIRM":
            g = "2_CONFIRM_notSTRONG"
        elif st == "ENTRY_WAIT" and wait == "WAIT_SELECTIVE_APPROVED":
            g = "3_WAIT_APPROVED"
        else:
            g = f"4_기타({st or 'MU없음'})"
        bars = [b for b in etf(d, e["sym"]) if e["t"][:5] <= b["datetime"][11:16] <= e["exits"][-1][0][:5]]
        mfe = (max(float(b["high"]) for b in bars) / e["px"] - 1) * 100 if bars else None
        mae = (min(float(b["low"]) for b in bars) / e["px"] - 1) * 100 if bars else None
        trades.append(dict(d=d, t=e["t"][:5], sym=e["sym"], pnl=e["pnl"], g=g, sid=sid[9:24], cttr=c, cs=cs,
                           exit=e["exits"][-1][3], mfe=mfe, mae=mae,
                           dir="RED" if e["sym"] == "0193T0" else "BLUE", am=e["t"] < "11:00"))
for x in trades:
    print(f"  {x['d'][4:]} {x['t']} {x['dir']:<4} {x['g']:<22} cttr={x['cttr']} {x['cs']:<8} {x['pnl']:>+10,} "
          f"MFE {x['mfe']:+.2f} MAE {x['mae']:+.2f} {x['exit']}")


def summ(rows):
    p = [x["pnl"] for x in rows]
    if not p:
        return "0건"
    w = [v for v in p if v > 0]; l = [v for v in p if v <= 0]
    s = sorted(p, reverse=True)
    pf = sum(w) / -sum(l) if l and sum(l) < 0 else float("inf")
    tp = sum(1 for x in rows if "TP" in x["exit"]); sl = sum(1 for x in rows if "STOP" in x["exit"] or "SL" in x["exit"])
    return (f"{len(p)}건 승률 {len(w)/len(p)*100:.0f}% 합 {sum(p):+,} PF {pf:.2f} 평균 {sum(p)/len(p):+,.0f} "
            f"MFE {sum(x['mfe'] for x in rows)/len(rows):+.2f} MAE {sum(x['mae'] for x in rows)/len(rows):+.2f} "
            f"TP {tp} / SL {sl} | 상위1제외 {sum(s[1:]):+,} 상위2제외 {sum(s[2:]):+,} | "
            f"오전 {sum(1 for x in rows if x['am'])} 오후 {sum(1 for x in rows if not x['am'])} | "
            f"RED {sum(1 for x in rows if x['dir']=='RED')} BLUE {sum(1 for x in rows if x['dir']=='BLUE')}")


print()
for g in sorted({x["g"] for x in trades}):
    print(f"  [{g}] {summ([x for x in trades if x['g'] == g])}")
print(f"  [전체] {summ(trades)}")
